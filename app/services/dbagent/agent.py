"""原生 function calling 的 ReAct 循环（2026-10-03 重写）。

替代原文本协议（"输出一个 JSON 动作"）：工具以 OpenAI schema 声明随请求
发给模型，模型回 tool_calls，代码执行后以 role=tool 消息回喂——协议正确性
由 API 保证，不再 brace 截取自解析。

十个工具：run_sql / write_sql / write_batch / get_metrics / list_recent_writes /
submit_plan（计划门，人工批准后再动手）/ request_more_steps（步数延长）/
rollback_change（变更回滚）/ recall_memory（会话记忆检索）/
submit_report（终点，过证据链）。
写三道门不变：guard_write_sql -> 人工审批 -> dbagent_writer 角色；批提案
（write_batch，P1-2）整组一次审批、单事务原子执行、账本共享 group_id。
系统提示恒定注入 schema 文本，故不再设 get_table_schema 工具（纯烧步数）。
"""

from __future__ import annotations

import json

from .evidence import EvidenceLog
from .step_budget import (  # 预算状态机已拆出；此处再导出保持 agent.XXX 引用不变
    EXTENSION_STEPS,
    GRACE_STEPS,
    MAX_STEP_EXTENSIONS,
    StepBudget,
)
from . import llm as llm_adapter

from .agent_prompts import (  # noqa: F401 批次 F 步骤 1：re-export 保持 agent.XXX 引用不变
    _TOOLS,
    _SYSTEM_PROMPT,
    build_metric_task,
    build_system_prompt,
    _PLANNER_PROMPT_PREFIX,
    _PLANNER_PROMPT_SUFFIX,
    _JSON_FENCE_RE
)

from .agent_ctx import (  # noqa: F401 批次 F 步骤 1：re-export 保持 agent.XXX 引用不变
    MaxStepsExceeded,
    TokenBudgetExceeded,
    WriteBlocked,
    _maybe_await,
    _RunCtx
)

from .agent_planner import (  # noqa: F401 批次 F 步骤 1：re-export 保持 agent.XXX 引用不变
    _parse_plan,
    _plan_context,
    _planner_plan,
    _run_planner
)

from .agent_tools import (  # noqa: F401 批次 F 步骤 1：re-export 保持 agent.XXX 引用不变
    _tool_recall_memory,
    _tool_run_sql,
    _tool_write_sql,
    _tool_write_batch,
    _tool_get_metrics,
    _detail_dict,
    _tool_list_recent_writes,
    _tool_submit_plan,
    _tool_rollback_change,
    _tool_submit_report
)

MAX_STEPS = 20


def _abandoned_result(ctx: "_RunCtx", log: EvidenceLog, line: str,
                      progress) -> dict:
    """废弃任务的收工结果（P2-2）：只回报告为空的结果，绝不 raise。

    墙钟终态已落库的僵尸协程没人收 result，raise 只会在事件循环留 exception 噪声；
    停步即不再发 LLM 调用、不再碰工具与门。2026-10-09 拆自 run_agent_task（门禁）。
    """
    progress(line)
    return {"report": "", "evidence_log": log, "usage": dict(ctx.usage),
            "abandoned": True}


def _require_token_budget(ctx: "_RunCtx") -> None:
    """P2-3 单任务 token 预算（input+output 累计，含重试尝试；0=不设限）。

    超限即抛 TokenBudgetExceeded -> _run_task 转 failed 留痕（usage 随终态落库）。
    """
    if not ctx.token_budget:
        return
    if ctx.usage.get("input", 0) + ctx.usage.get("output", 0) <= ctx.token_budget:
        return
    raise TokenBudgetExceeded(
        f"token 预算 {ctx.token_budget} 用尽"
        f"（input {ctx.usage.get('input', 0)} + output "
        f"{ctx.usage.get('output', 0)}，{ctx.usage.get('calls', 0)} 次调用）"
        "。任务转 failed 留痕，请缩小问题范围后重新提交。")


async def _ask_llm(messages: list[dict], ctx: "_RunCtx", log: EvidenceLog,
                   progress) -> tuple[dict | None, dict | None]:
    """一次 LLM 调用 + 预算与废弃两道复查（2026-10-09 拆自 run_agent_task）。

    返回 (message, None) 表示可继续；返回 (None, 终态结果) 表示僵尸循环须停步——
    一次调用可达分钟级，其间墙钟可能已把任务终态落库（2026-10-07 C3/P1-8），
    只查循环顶部会让僵尸继续跑工具（查询/写门/续步）并接着烧钱。
    """
    message = await llm_adapter.chat(messages, tools=_TOOLS, usage_out=ctx.usage)
    _require_token_budget(ctx)
    if ctx.should_stop is not None and ctx.should_stop():
        return None, _abandoned_result(
            ctx, log, "任务已废弃（LLM 步返回后复查），僵尸循环停步", progress)
    return message, None


async def run_agent_task(task: str, db_fetch, schema_text: str,
                         max_steps: int = MAX_STEPS,
                         db_write=None, confirm_write=None, confirm_plan=None,
                         propose_only: bool = False,
                         db_write_batch=None, confirm_write_batch=None,
                         verbose_cb=None, forced_propose=None,
                         should_stop=None, token_budget: int = 0,
                         approval_actor=None, memory=None,
                         allow_delete: bool = True,
                         actor: str = "", is_admin: bool = False,
                         planner: bool = False) -> dict:
    """跑一次任务。返回 {report, evidence_log, steps}。

    db_fetch: async (sql, params=()) -> list[dict]；db_write: async (sql, params)
    （传 None 即写通道关闭）。confirm_write/confirm_plan 可同步可异步，None 视为否决
    （confirm_plan 为 None 时计划门降级为直接放行）。propose_only: 影子期——写门
    批准后 db_write 只登记不执行（见 _tool_write_sql）。db_write_batch/confirm_write_batch:
    批提案组通道（P1-2，None = 批通道关闭）。verbose_cb: 每步一行进度文本。
    forced_propose: () -> bool（P1-5），L2/unknown 高危提案强制转人工的判定，
    在写门批准后求值——真值来源是门暂存的风险等级（tasks._GATES）。
    should_stop: () -> bool（P2-2），每步循环头求值，True 即停——墙钟终态
    落库后的僵尸协程确定性终止（cancel 被 httpx/anyio 吸收也不受影响）。
    token_budget: 单任务 input+output 累计上限（P2-3，0=不设限），超限抛
    TokenBudgetExceeded -> 任务转 failed 留痕。
    allow_delete: 生成前 RBAC 动词闸（生产评审⑥）——False 时 DELETE 提案
    在守卫即拒；actor/is_admin: 提交人身份（回滚归属校验与审计口径用），
    CLI/评测等受信路径可缺省（不做归属限制）。
    planner: Planner-Executor 拆分（业主 2026-10-05 批准）——True 时先由
    规划者独立调用产出结构化计划、经计划门人工批准后注入执行上下文；
    产出不可解析或被否决时退回自主执行（写时仍自动过计划门）。
    """
    def progress(line: str) -> None:
        if verbose_cb:
            verbose_cb(line)

    log = EvidenceLog(task_text=task)
    ctx = _RunCtx(db_fetch, db_write, confirm_write, confirm_plan, log, progress,
                  propose_only, db_write_batch, confirm_write_batch,
                  forced_propose, should_stop, token_budget, approval_actor,
                  memory, allow_delete, actor, is_admin)
    messages: list[dict] = [
        {"role": "system", "content": build_system_prompt(schema_text)},
        {"role": "user", "content": task},
    ]
    if planner:
        # 规划者先行：结构化计划过计划门（人批分解），批准后注入执行上下文
        plan = await _planner_plan(task, schema_text, ctx.usage, progress)
        if plan:
            await _run_planner(messages, ctx, plan, progress)
    budget = StepBudget(max_steps)
    step = 0
    while True:
        step += 1
        if budget.exhausted(step):
            break
        if ctx.should_stop is not None and ctx.should_stop():
            return _abandoned_result(
                ctx, log, "任务已废弃（墙钟终态已落），僵尸循环停步", progress)
        grace = budget.grace_prompt(step)
        if grace:
            messages.append({"role": "user", "content": grace})
            progress("步数预算用尽｜等待模型决定收尾或申请延长")
        message, finished = await _ask_llm(messages, ctx, log, progress)
        if finished is not None:
            return finished
        tool_calls = [c for c in (message.get("tool_calls") or [])
                      if isinstance(c, dict)]
        if not tool_calls:
            _nudge_without_tool(message, messages, progress)
            continue
        messages.append({"role": "assistant",
                         "content": str(message.get("content") or ""),
                         "tool_calls": tool_calls})
        finished = await _run_tool_calls(tool_calls, messages, ctx, budget,
                                         step, progress)
        if finished is not None:
            return finished
    raise MaxStepsExceeded(
        f"{budget.total} 步预算与延长次数用尽，未交出合格报告。已完成证据：\n{log.transcript()}")


def _nudge_without_tool(message: dict, messages: list[dict], progress) -> None:
    """模型没调工具时引导收口，不强推（2026-10-09 拆出自 run_agent_task 循环体）。

    管家式对话（能力问询/闲聊）本身就是合法回答，引导其走 submit_report 收口；
    确实没答完的模型会自行转去查数。
    """
    messages.append({"role": "assistant",
                     "content": str(message.get("content") or "")})
    messages.append({"role": "user", "content": (
        "如果上面已经是给用户的最终回答，调用 submit_report 原样提交；"
        "如果还需要查询数据才能回答，调用相应查询工具。")})
    progress("提醒｜模型未调工具，已引导其收口或继续查数")


async def _run_tool_calls(tool_calls: list[dict], messages: list[dict],
                          ctx: _RunCtx, budget: StepBudget, step: int,
                          progress) -> dict | None:
    """顺序执行本步的工具调用，回喂观察；有终态结果就返回它（2026-10-09 拆出）。"""
    for call in tool_calls:  # DeepSeek 单工具为常态；多调用防御性顺序执行
        fname = str((call.get("function") or {}).get("name") or "")
        observation, finished = await _handle_tool_call(call, ctx, budget, step)
        if finished is not None:
            return finished
        messages.append({"role": "tool", "tool_call_id": call.get("id"),
                         "content": observation})
        if fname != "request_more_steps":
            progress(f"{fname}｜观察：{observation[:160]}")
    return None


async def _handle_tool_call(call: dict, ctx: _RunCtx, budget: StepBudget,
                            step: int) -> tuple[str, dict | None]:
    """执行一次工具调用，返回（回喂模型的观察, 终态结果）。结果非 None 即收工。

    顺序即语义：request_more_steps 在重复检测之前处理（连续两次申请是合法的，
    且必须重置重复基线，否则第二次会被当成重复调用拦掉）；重复拦截只回喂警告、
    不更新基线，好让模型下一次换思路的调用不被误判。
    """
    fname = str((call.get("function") or {}).get("name") or "")
    raw_args = str((call.get("function") or {}).get("arguments") or "{}")
    try:
        args = json.loads(raw_args)
        if not isinstance(args, dict):
            raise ValueError("参数不是 JSON 对象")
    except (ValueError, json.JSONDecodeError):
        return "工具参数不是合法 JSON 对象，请重发该调用。", None
    if fname == "request_more_steps":
        observation, progress_line = budget.grant(args)
        if progress_line:
            ctx.progress(progress_line)
        ctx.last_call = None
        return observation, None
    if ctx.duplicate(fname, args):
        ctx.progress(f"{fname}｜重复调用已拦截")
        return ("警告：与上一步完全相同的调用，已跳过执行。"
                "请改变思路，或用 submit_report 交报告。", None)
    observation, finished = await _execute_tool(fname, args, ctx)
    if finished is not None:
        finished["steps"] = step
        ctx.progress(f"完成｜共 {step} 步，证据 {len(ctx.log._items)} 条")
        return observation, finished
    # 临预算提醒：收尾或申请延长，二选一（不强制、只提示）
    return observation + (budget.nearing(step, fname) or ""), None


async def _execute_tool(name: str, args: dict, ctx: _RunCtx) -> tuple[str, dict | None]:
    """分派执行一个工具。返回 (观察文本, 最终结果)；结果非 None 时循环终止。"""
    if name == "run_sql":
        return await _tool_run_sql(args, ctx), None
    if name == "write_sql":
        return await _tool_write_sql(args, ctx), None
    if name == "write_batch":
        return await _tool_write_batch(args, ctx), None
    if name == "get_metrics":
        return await _tool_get_metrics(args, ctx), None
    if name == "list_recent_writes":
        return await _tool_list_recent_writes(args, ctx), None
    if name == "submit_plan":
        return await _tool_submit_plan(args, ctx), None
    if name == "rollback_change":
        return await _tool_rollback_change(args, ctx), None
    if name == "recall_memory":
        return await _tool_recall_memory(args, ctx), None
    if name == "submit_report":
        return _tool_submit_report(args, ctx)
    return f"未知工具 {name!r}。可用：run_sql / write_sql / write_batch / get_metrics / list_recent_writes / submit_plan / rollback_change / recall_memory / request_more_steps / submit_report。", None

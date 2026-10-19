"""Agent 简单任务的 LLM 格式化与收尾原语 —— 从 router.py 拆出（2026-09-14）。

router.py 加上格式化降级视图后达 622 行（硬限 600），按子系统边界搬迁。
本模块只含「格式化与落盘」：消息组装、单次 LLM 格式化（带计量扣费）、
DFA 收口、可读降级视图、记忆/缓存落盘。工具执行与编排仍归 router.py。
"""

import json
from typing import Optional

import httpx

from ..core.logging import setup_logging

logger = setup_logging()


def _build_formatter_messages(system: str, user_query: str, history: list | None) -> list:
    """组装简单任务 LLM 消息序：system + 会话历史（含滚动摘要 system 条）+ 当前 user"""
    msgs = [{"role": "system", "content": system}]
    msgs.extend(history or [])
    msgs.append({"role": "user", "content": user_query})
    return msgs


async def _format_via_llm(api_key: str, system: str, user_query: str,
                          username: str = "", history: list | None = None) -> Optional[str]:
    """简单任务第 2 步：单次 LLM 格式化（带 token 计量）。

    失败返回 None，调用方降级用工具原始结果，不让任务报错。
    username 供扣费（2026-09-09 审查 P0 收口：简单任务原先与 runner 同款只打指标）。
    history：会话历史（MemoryManager.get_context 产出，含滚动摘要 system 条）。
    2026-09-10 修复：简单工具通道此前只写记忆不读——"我前面问了哪座城市"这类
    追问每条都失忆（主聊天通道同源 get_context，本通道漏接）。
    """
    from ..core.config import (
        LLM_PRIMARY_MODEL,
        HTTP_TIMEOUT_MEDIUM,
        apply_llm_request_options,
        llm_endpoint,
    )
    from ..core.metrics import llm_tokens_total, llm_tokens_detail, llm_requests_total
    # 主力模型可能是 qwen（百炼端点），按模型名路由端点与密钥
    base_url, api_key = llm_endpoint(LLM_PRIMARY_MODEL, api_key)
    messages = _build_formatter_messages(system, user_query, history)
    try:
        # 纳入全局 LLM 并发闸（2026-09-11 规则审查 P1：简单任务格式化是高频路径，
        # 原先直连可绕过全局并发控制）
        from ..core.concurrency import llm_semaphore

        async def _post():
            async with llm_semaphore:
                async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_MEDIUM) as client:
                    return await client.post(
                        f"{base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json=apply_llm_request_options({
                            "model": LLM_PRIMARY_MODEL,
                            "messages": messages,
                            "temperature": 0.3,
                            "max_tokens": 1024
                        }, LLM_PRIMARY_MODEL)
                    )

        resp = await _post()
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]

        # Token 计量
        # 扣费幂等键（P1-②，2026-10-11）：本次即一次上游调用，一次一个键
        from ..services.chat_support import attach_bill_uid
        attach_bill_uid(data.get("usage"))
        usage = data.get("usage", {})
        pt = usage.get("prompt_tokens", 0) or 0
        ct = usage.get("completion_tokens", 0) or 0
        if pt or ct:
            llm_tokens_total.labels(type='input').inc(pt)
            llm_tokens_total.labels(type='output').inc(ct)
            llm_tokens_detail.labels(model=LLM_PRIMARY_MODEL, endpoint='simple_task', type='input').inc(pt)
            llm_tokens_detail.labels(model=LLM_PRIMARY_MODEL, endpoint='simple_task', type='output').inc(ct)
        llm_requests_total.labels(model=LLM_PRIMARY_MODEL, endpoint='simple_task', status='success').inc()
        # 扣费收口（2026-09-09 审查 P0）：指标已由上方 simple_task 打点，
        # 复用不含指标的 bill_token_usage 补钱包扣费与日 token 计数
        if username and (pt or ct):
            from ..services.llm_streaming import bill_token_usage
            bill_token_usage(usage, username, "",
                             remark=f"Agent任务消耗 {pt + ct} tokens（输入 {pt} + 输出 {ct}）")
        return content
    except Exception as e:
        logger.warning(f"简单任务 LLM 格式化失败: {e}")
        return None


async def _load_file_ctx(file_ids, username: str = ""):
    """上传文件上下文（原实现直接忽略 file_ids，简单路径看不到文件内容）；无文件返回 None"""
    if not file_ids:
        return None
    from types import SimpleNamespace
    from ..core.stream_utils import build_file_context
    return await build_file_context(SimpleNamespace(file_ids=file_ids), username)


async def _safe_history(mm) -> list:
    """读取会话历史（与主聊天通道同源 get_context：Redis 热缓存 + PG 回源 + 滚动摘要）。

    2026-09-10 修复：简单工具通道此前只写记忆不读——"我前面问了哪座城市"这类
    追问每条都失忆。读取失败降级无历史，不阻塞工具问答主流程。
    """
    try:
        return await mm.get_context()
    except Exception as e:
        logger.warning(f"简单任务读取会话历史失败（降级无历史）: {e}")
        return []


async def _emit_simple_result(task_id: str, final_answer: str) -> str | None:
    """步骤 3 收口：DFA 检查 → 分块写 Redis → 状态完成。返回过滤后文本。

    2026-09-12 修复（外部复核 P0）：调用方需要用同一份过滤后文本写记忆和
    语义缓存——原实现只在内部过滤后写 Redis，调用方仍持过滤前原文本。
    2026-09-15 修复（B-11）：返回 None 表示 finish_task 的终态 CAS 失败
    （任务已被取消/超时收口）——调用方必须停止，不得再写记忆与语义缓存，
    与 runner 主路径 `completed = await finish_task(...); if not completed: return`
    同一契约。
    """
    from ..core.task_manager import append_result, finish_task
    from ..services.llm_streaming import apply_safety_filter
    final_answer = apply_safety_filter(final_answer)
    for i in range(0, len(final_answer), 80):
        await append_result(task_id, final_answer[i:i+80])
    # 必须 await：漏掉 await 会产生未执行的 coroutine，任务永久停在 generating。
    completed = await finish_task(task_id, final_answer)
    if not completed:
        return None
    return final_answer


async def _deliver_simple_answer(mm, task_id: str, user_query: str,
                                 final_answer: str, cache_allowed: bool,
                                 cache_ctx: str | None, agent_name: str,
                                 persona_id: str = "") -> bool:
    """步骤 3 整体收口：emit → 拒答则禁缓存 → 存记忆/语义缓存（2026-10-09 拆出自 handle_simple_task）。

    返回 False 表示 finish_task 终态 CAS 失败（任务已被取消/超时收口，B-11），
    调用方必须直接结束，不得再写记忆与语义缓存。
    """
    emitted = await _emit_simple_result(task_id, final_answer)
    if emitted is None:
        logger.info(f"简单任务终态已被收口，跳过记忆/缓存写入: {task_id}")
        return False
    # _emit_simple_result 内 apply_safety_filter 命中会把整段换成 safe_message
    # （拒答文案）；与 runner 主路 state["blocked"]→write_cache=False 同口径（C1/P1-3）
    if emitted != final_answer:
        cache_allowed = False
    await _save_simple_task_artifacts(
        mm, task_id, user_query, emitted, cache_ctx, agent_name,
        persona_id, write_cache=cache_allowed,
    )
    return True


def _fallback_tool_view(tool_result) -> str:
    """LLM 格式化失败时的可读降级视图（2026-09-14 审计 P1）。

    原实现把工具原始 JSON 直接 json.dumps 给用户——内部结构外泄，且该 JSON
    会经 _emit_simple_result 写进语义缓存钉死 60 天。此处渲染为"字段: 值"
    行式视图；错误结果换友好文案。
    """
    if not isinstance(tool_result, dict):
        return "数据已获取，但展示服务暂时不可用，请稍后重试。"
    err = tool_result.get("error")
    if err:
        err = str(err)
        if "未找到" in err:
            return "暂时没找到这个城市的数据，建议换个关键词试试。"
        if "超时" in err:
            return "查询超时，请稍后再试。"
        return "获取数据失败了，可能服务暂时不可用，请稍后再试。"
    lines = []
    for key, value in tool_result.items():
        if key.startswith("_"):
            continue  # 内部字段不外泄
        if isinstance(value, (list, dict)):
            value = json.dumps(value, ensure_ascii=False)
        lines.append(f"{key}：{value}")
    return "\n".join(lines) or "数据已获取，但展示服务暂时不可用，请稍后重试。"


async def _save_simple_task_artifacts(mm, task_id: str, user_query: str,
                                      final_answer: str, cache_ctx: str | None,
                                      agent_name: str, persona_id: str = "",
                                      write_cache: bool = True) -> None:
    """简单任务收尾：保存记忆 + 写语义缓存（2026-09-12 拆出自 handle_simple_task）。

    write_cache=False 用于降级/拒答文案（2026-10-07 C1/P1-3）：语义缓存只认
    query 相似不认文案性质，兜底文案一旦钉住 60 天，同问全部命中不可用提示。
    """
    await mm.save_messages(
        {"role": "user", "content": user_query},
        {"role": "assistant", "content": final_answer}
    )
    try:
        from ..services.answer_trace import record_answer
        # persona 空值兜底用 "default"（2026-09-28 R2）：原兜底 "civil_code" 会把
        # 无人格归属的普通任务答案记进民事基准的答案侧，评测集被通用流量污染
        record_answer(
            user_query, final_answer, getattr(mm, "user_id", ""),
            persona=persona_id or "default",
        )
    except Exception as e:
        logger.debug(f"简单任务答案 trace 创建失败（不影响主流程）: {e}")
    if cache_ctx is not None and write_cache:
        # spawn 持强引用，防止缓存写入任务被 GC 中途回收。
        from ..core.semantic_cache import safe_set
        from ..core.concurrency import spawn
        spawn(safe_set(user_query, final_answer, cache_ctx=cache_ctx),
              name=f"semantic-cache:{task_id}")
    logger.info(f"简单任务完成: task_id={task_id}, agent={agent_name}")

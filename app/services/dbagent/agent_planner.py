"""规划者：独立一次调用产出结构化计划，经计划门人批后注入执行上下文（批次 F 步骤 1 自 agent.py 拆出）。
"""
from __future__ import annotations

import json

from .catalog import context_text
from . import llm as llm_adapter
from .agent_ctx import _RunCtx, _maybe_await
from .agent_prompts import (_JSON_FENCE_RE, _PLANNER_PROMPT_PREFIX,
                            _PLANNER_PROMPT_SUFFIX)


def _parse_plan(text: str) -> dict | None:
    """规划者输出的 JSON 解析（纯函数锚点）：剥围栏 → 截取 JSON 体 → 校验形状。

    goal 一句话非空、steps 2-8 且每步有 title；不合形返回 None（执行者退回
    自主规划，绝不因规划失败阻塞任务）。"""
    raw = (text or "").strip()
    fence = _JSON_FENCE_RE.search(raw)
    if fence:
        raw = fence.group(1).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start:end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    steps = data.get("steps")
    if not isinstance(steps, list) or not 2 <= len(steps) <= 8:
        return None
    clean = []
    for s in steps:
        if not isinstance(s, dict) or not str(s.get("title") or "").strip():
            return None
        clean.append({"title": str(s["title"]).strip()[:120],
                      "approach": str(s.get("approach") or "").strip()[:200]})
    goal = str(data.get("goal") or "").strip()[:200]
    if not goal:
        return None
    return {"goal": goal, "steps": clean}


def _plan_context(plan: dict) -> str:
    """批准后的计划注入文本（执行者上下文）：计划是指导不是死命令。"""
    lines = [f"[执行计划（已经人工批准，共 {len(plan['steps'])} 步）]"]
    lines += [f"{i}. {s['title']}：{s['approach']}"
              for i, s in enumerate(plan["steps"], 1)]
    lines.append("按顺序推进，每步完成简述结论再进入下一步；"
                 "执行中发现与计划不符时如实调整并说明原因。")
    return "\n".join(lines)


async def _planner_plan(task: str, schema_text: str, usage_out: dict,
                        progress) -> dict | None:
    """规划者阶段：独立 LLM 调用产出结构化计划——只规划不查询（业务目录与
    schema 已在上下文，查数与验证是执行者的职责）。产出不可解析/调用失败
    返回 None，执行者退回自主规划；计划经既有计划门人工批准后注入执行
    上下文。规划调用与主循环同一计量入口（usage_out，B5 口径）。"""
    prompt = (_PLANNER_PROMPT_PREFIX + "\n业务目录与口径：\n" + context_text()
              + "\n\n表结构（列名逐字可用）：\n" + schema_text
              + "\n\n任务：" + task + _PLANNER_PROMPT_SUFFIX)
    try:
        message = await llm_adapter.chat([{"role": "user", "content": prompt}],
                                         max_tokens=800, temperature=0.0,
                                         usage_out=usage_out)
    except Exception as exc:  # noqa: BLE001 — 规划失败不阻塞任务
        progress(f"规划｜规划调用失败（{type(exc).__name__}），退回自主执行")
        return None
    plan = _parse_plan(str(message.get("content") or ""))
    if plan is None:
        progress("规划｜产出不可解析，退回自主执行")
    return plan


async def _run_planner(messages: list[dict], ctx: _RunCtx, plan: dict,
                       progress) -> None:
    """计划过计划门并注入执行上下文（Planner-Executor 拆分的门联动）。

    计划门批准即置 plan_approved——写时不再重复自动代提计划门（一次人工
    批准覆盖"分解合理"与"计划可行"两语义；写门仍独立审批命令本身）。
    被否决则转自主执行，写时仍自动过计划门（既有硬闸不变）。"""
    gate_steps = [f"{s['title']}：{s['approach']}"[:150] for s in plan["steps"]]
    if ctx.confirm_plan is None:
        ctx.plan_approved = True  # CLI 无门通道：与 submit_plan 降级一致
    else:
        ctx.plan_approved = bool(await _maybe_await(
            ctx.confirm_plan(plan["goal"], gate_steps)))
    if ctx.plan_approved:
        progress(f"规划｜计划{'已获人工批准' if ctx.confirm_plan else '直接注入'}"
                 f"（{len(plan['steps'])} 步），执行者按步推进")
        messages.append({"role": "user", "content": _plan_context(plan)})
    else:
        progress("规划｜计划被人工否决，转自主执行（写时仍自动过计划门）")

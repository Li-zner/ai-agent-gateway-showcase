"""DB Agent 的 LLM 适配层：复用网关 chat_support（超时/flash 回退/端点路由）。

2026-10-03 走原生 function calling：payload 透传 tools/tool_choice
（post_chat_completion 对请求体无白名单过滤，字段原样到达上游），
返回完整 message dict（含 tool_calls）。纯工具调用响应 content 为空是合法形态，
不再是失败——"空正文即失败"只对无 tool_calls 的响应生效。

通道阶梯（P1-1，业主 2026-10-04 定稿、10-08 按官方价目改末级）：主力
deepseek-flash（DeepSeek）→ 备选 qwen3.7-flash（百炼）→ 兜底 qwen3.7-flash×2
（原 qwen3.8-flash 单价高约 4 倍，改 3.7 后末两级同名，读作同端点重试两轮）。阶梯显式写在
_CHANNELS，不经 post_chat_completion 的默认 try-list（其中 LLM_PRIMARY_MODEL
槽位历史上装的是 qwen 系，语义已倒挂）。兜底给两轮：真上游偶发抖动在长任务
里被步数放大（断通道演练实测 205 次调用失败 4 次，≈2%）。全挂就如实失败，
宁可不交不乱交。
"""
from __future__ import annotations

import faulthandler
import logging

from ...core.config import (
    LLM_FALLBACK_MODEL,
    LLM_PRIMARY_MODEL,
    HTTP_TIMEOUT_MEDIUM,
    LLM_LAST_RESORT_MODEL,
)
from ..chat_support import post_chat_completion
from ...core.metrics import dbagent_llm_fallbacks_total, dbagent_llm_tokens_total

logger = logging.getLogger("rag_console.dbagent")

# 有序通道（去重由 post_chat_completion 处理）：主力/备选/兜底×2
_CHANNELS: tuple[str, ...] = (
    LLM_FALLBACK_MODEL,   # 主力：DeepSeek（deepseek-flash）
    LLM_PRIMARY_MODEL,         # 备选：qwen3.7-flash（百炼）
    LLM_LAST_RESORT_MODEL,     # 兜底：qwen3.7-flash（百炼，与备选同名＝同端点重试）
    LLM_LAST_RESORT_MODEL,
)

# faulthandler 的 later 定时器全进程只有一支：并发任务各自 arm/cancel 会互相顶掉，
# 真挂死时恰好可能没人 armed（10-08 台账 456）。改成引用计数：0→1 才开表，1→0 才关。
# ponytail: 计数假设单事件循环（应用与评测脚本都是）；多 loop 并发需换 threading.Lock。
_HANG_WATCHDOG_SECONDS = 60
_watchdog_depth = 0


def _arm_hang_watchdog() -> None:
    global _watchdog_depth
    _watchdog_depth += 1
    if _watchdog_depth == 1:
        faulthandler.dump_traceback_later(_HANG_WATCHDOG_SECONDS, exit=False)


def _disarm_hang_watchdog() -> None:
    global _watchdog_depth
    _watchdog_depth = max(0, _watchdog_depth - 1)
    if _watchdog_depth == 0:
        faulthandler.cancel_dump_traceback_later()


def _extract_message(data: dict) -> dict:
    """从 OpenAI 兼容响应里取 choices[0].message（含 content 与 tool_calls）。"""
    choices = data.get("choices") or [{}]
    message = choices[0].get("message") or {}
    return message if isinstance(message, dict) else {}


async def chat(messages: list[dict], tools: list[dict] | None = None,
               max_tokens: int = 1500, temperature: float = 0.0,
               usage_out: dict | None = None) -> dict:
    """单次非流式调用，返回 message dict（{content, tool_calls?}）。

    失败条件：全部通道（主通道两轮 + 兜底两轮）上游不可用或空正文。
    空内容偶发（推理段吃预算），重试一次再判失败。
    usage_out 传入时累计本任务的 token 消耗（含重试尝试）并打 Prometheus 计数。
    """
    payload: dict = {"model": None, "messages": messages,
                     "temperature": temperature, "max_tokens": max_tokens}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    last_error = ""
    fallback_used = False
    for round_idx, model in enumerate(_CHANNELS):
        if round_idx >= 2:
            fallback_used = True
            dbagent_llm_fallbacks_total.labels("invoked").inc()
        # 60s 看门狗：挂起时自动往 stderr 打 Python 栈（间歇挂死诊断，#6 遗留）
        _arm_hang_watchdog()
        try:
            ok, data = await post_chat_completion(
                payload, timeout=HTTP_TIMEOUT_MEDIUM, models=[model])
        finally:
            _disarm_hang_watchdog()
        if ok:
            usage = (data.get("usage") or {}) if isinstance(data, dict) else {}
            pin, pout = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
            if pin or pout:
                dbagent_llm_tokens_total.labels("input").inc(pin)
                dbagent_llm_tokens_total.labels("output").inc(pout)
                if usage_out is not None:
                    usage_out["input"] = usage_out.get("input", 0) + pin
                    usage_out["output"] = usage_out.get("output", 0) + pout
                    usage_out["calls"] = usage_out.get("calls", 0) + 1
            message = _extract_message(data)
            if bool(message.get("tool_calls")) or str(message.get("content") or "").strip():
                if fallback_used:
                    dbagent_llm_fallbacks_total.labels("success").inc()
                return message
            last_error = "LLM 返回空内容（预算可能被推理段吃掉）"
        else:
            last_error = "上游 LLM 不可用"
            # 诊断留痕（通道排障用；不含密钥）：status/body 是 post_chat_completion 的诊断信封
            logger.warning("DB Agent LLM 通道失败 round=%d model=%s status=%s body=%s",
                           round_idx + 1, model,
                           (data or {}).get("status"), str((data or {}).get("body"))[:120])
    if fallback_used:
        dbagent_llm_fallbacks_total.labels("failed").inc()
    raise RuntimeError(last_error)

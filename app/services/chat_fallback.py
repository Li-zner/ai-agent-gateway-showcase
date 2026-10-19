"""对话降级链：主模型失败后的 Flash 备胎（单级降级，Dify 已彻底摒弃）

抽取自 app/routes/v2.py（2026-09 重构，行为等价纯移动）。
"""
import asyncio
import json
import uuid
from typing import AsyncIterator

import httpx

from ..models.schemas import ChatRequest
from ..core.config import (
    DEEPSEEK_API_BASE,
    LLM_FALLBACK_MODEL, HTTP_TIMEOUT_MEDIUM,
)
from ..core.concurrency import llm_semaphore
from ..core.jfast import loads as jloads
from ..core.logging import setup_logging
from ..core.persona_manager import is_civil_persona
from ..core.safety_filter import ContentStreamGuard, get_filter
from ..core.semantic_cache import SemanticCache
from ..core.stream_utils import sse
from .civil_grounding import grounding_reply
from .chat_support import (
    attach_bill_uid, get_deepseek_key, lang_instruction, mark_key_result,
    safety_message, fallback_message,
)

logger = setup_logging()


async def fallback_flash(req: ChatRequest, username: str) -> AsyncIterator[str]:
    """第一级降级：使用 DeepSeek Flash（无工具调用，纯聊天）"""
    if is_civil_persona(getattr(req, "persona_id", "")):
        return
    from ..core.metrics import gateway_errors_total
    gateway_errors_total.labels(status='fallback_flash').inc()
    logger.info(f"触发 Flash 降级: username={username}")
    deepseek_api_key = await get_deepseek_key()
    if not deepseek_api_key:
        logger.warning("Flash 降级无 API Key，跳过")
        return
    _yielded = False  # 是否已产出内容：中途失败时决定本层补 [DONE] 还是交 chain 兜底
    # 本次上游调用一个扣费幂等键（P1-②，2026-10-11）：真实 usage 尾块与估算兜底同属这一笔
    _bill_uid = uuid.uuid4().hex
    try:
        async for chunk in _flash_sse(req, username, deepseek_api_key, _bill_uid):
            _yielded = True
            yield chunk
    except Exception as e:
        await mark_key_result(deepseek_api_key, False)
        logger.warning(f"Flash 降级失败: {e}")
        if _yielded:
            # 中途失败且已产出部分内容：本层补 [DONE] 终结事件防前端悬挂
            # （原先异常在此被吞，fallback_chain 的 [DONE] 补发分支永不触发，2026-09-09 审查 P1）
            yield "data: [DONE]\n\n"
        # 未产出任何内容：静默返回，交 fallback_chain 空流兜底（answer_complete+[DONE]）


def _flash_payload(req: ChatRequest, username: str) -> dict:
    """构造 Flash 降级请求体（2026-09-11 拆出自 _flash_sse，满足 80 行函数门禁）"""
    return {
        "model": LLM_FALLBACK_MODEL,
        "messages": [
            {"role": "system", "content": "你是AI助手，请直接回答用户的问题。如果涉及法律问题，回答相关法律规定；如果无法查询知识库，请如实说明暂无法核实依据，不要编造具体条文号。" + lang_instruction(req)},
            {"role": "user", "content": req.query}
        ],
        "user": username,
        "stream": True,
        "stream_options": {"include_usage": True},  # 2026-09-09 审查 P1：降级链补计量
        "temperature": 0.3,
        "max_tokens": 2048,
    }


async def _bill_intercepted(_usage, username: str, full_text: str,
                            record_token_usage, _estimate_tokens, bill_uid: str = "") -> None:
    """DFA 拦截 return 前计费（2026-09-11 P1）：token 已真实消耗。
    usage 尾块未到时按已产出文本估算（与断连口径一致）。拆出以满足 80 行门禁。
    bill_uid（P1-②，2026-10-11）：本次上游调用的幂等键，估算与真实尾块共用。"""
    if _usage:
        record_token_usage(attach_bill_uid(_usage, bill_uid), username, "")
    elif username and full_text:
        est = _estimate_tokens(full_text)
        record_token_usage(
            attach_bill_uid({"prompt_tokens": 0, "completion_tokens": est}, bill_uid),
            username, "",
            remark=f"DFA拦截估算计费（Flash降级 {est} tokens）")


def _parse_flash_chunk(data_str: str, guard: ContentStreamGuard) -> tuple:
    """解析一条 Flash SSE 数据，返回用量、原始块、安全块和拦截状态。"""
    data = jloads(data_str)
    if not isinstance(data, dict):
        # 合法 JSON 但不是对象（null / [] / 数字）：按坏行同待遇交调用方跳过，
        # 不能让 AttributeError 打断末层降级（10-07 审查 P2，10-06 orchestrator 同族）
        raise ValueError(f"Flash SSE 非对象 JSON: {type(data).__name__}")
    usage = data.get("usage")
    choices = data.get("choices")
    first = choices[0] if isinstance(choices, list) and choices else {}
    delta = first.get("delta") if isinstance(first, dict) else None
    chunk = delta.get("content", "") if isinstance(delta, dict) else ""
    if not chunk:
        return usage, "", "", False
    piece, blocked = guard.feed(chunk)
    return usage, chunk, piece, blocked


async def _flash_sse(req: ChatRequest, username: str, deepseek_api_key: str, bill_uid: str = "") -> AsyncIterator[str]:
    """Flash SSE 消费主体：跨块 DFA 守卫 + include_usage 计量（异常向上传播）；bill_uid=本次上游调用的扣费幂等键（P1-②，两处估算兜底与真实尾块共用）"""
    from .llm_streaming import _estimate_tokens, record_token_usage
    async with llm_semaphore:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_MEDIUM) as client:
            async with client.stream(
                "POST",
                f"{DEEPSEEK_API_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {deepseek_api_key}", "Content-Type": "application/json"},
                json=_flash_payload(req, username),
            ) as resp:
                resp.raise_for_status()
                await mark_key_result(deepseek_api_key, True)
                sf = get_filter()
                _guard = ContentStreamGuard(sf)
                _full_text = ""  # 原始全文，供拦截计费估算
                _safe_text = ""  # 实际外发全文，供 answer_complete 收口
                _usage = None    # include_usage 尾块携带的 token 用量（计量扣费用）
                try:
                    async for line in resp.aiter_lines():
                        if line.startswith("data:"):
                            data_str = line[5:].strip()
                            if data_str == "[DONE]":
                                break
                            try:
                                # include_usage 尾块 choices 为空，解析器已容忍空数组
                                _new_usage, chunk, _piece, _blocked = _parse_flash_chunk(
                                    data_str, _guard)
                                if _new_usage:
                                    _usage = attach_bill_uid(_new_usage, bill_uid)
                                if chunk:
                                    _full_text += chunk
                                    if _blocked:
                                        logger.warning("DFA 拦截Flash降级: 跨块或单块命中")
                                        _safe_text += safety_message(req, sf.safe_message)
                                        yield sse("answer_chunk", safety_message(req, sf.safe_message))
                                        await _bill_intercepted(
                                            _usage, username, _full_text,
                                            record_token_usage, _estimate_tokens, bill_uid)
                                        yield sse("answer_complete", _safe_text) + "data: [DONE]\n\n"
                                        return
                                    if _piece:
                                        _safe_text += _piece
                                        yield sse("answer_chunk", _piece)
                            except (json.JSONDecodeError, ValueError) as e:
                                logger.debug(f"SSE 行解析失败跳过: {e}")
                except (GeneratorExit, asyncio.CancelledError):
                    # 断连估算计费（2026-09-09 审查 P1 修复）：直连 httpx 无法安全排空
                    # （async with 随生成器关闭，排空任务会撞上已关闭的 resp），退化按
                    # 已产出文本估算，口径见 llm_streaming._estimate_tokens
                    if username and _full_text:
                        record_token_usage(
                            attach_bill_uid({"prompt_tokens": 0, "completion_tokens": _estimate_tokens(_full_text)}, bill_uid),
                            username, "",
                            remark=f"断连估算补计费（Flash降级 {_estimate_tokens(_full_text)} tokens）")
                    raise
                _tail, _tail_blocked = _guard.flush()
                if _tail_blocked:
                    logger.warning("DFA 流尾拦截Flash降级")
                    _safe_text += safety_message(req, sf.safe_message)
                    yield sse("answer_chunk", safety_message(req, sf.safe_message))
                    await _bill_intercepted(
                        _usage, username, _full_text,
                        record_token_usage, _estimate_tokens, bill_uid)
                    yield sse("answer_complete", _safe_text) + "data: [DONE]\n\n"
                    return
                if _tail:
                    _safe_text += _tail
                    yield sse("answer_chunk", _tail)
                # 计量扣费（2026-09-09 审查 P1）：正常路径按真实 usage 计费；
                # 拦截路径已由 _bill_intercepted 提前完成，避免 usage 双计。
                if _usage:
                    record_token_usage(_usage, username, "", model=LLM_FALLBACK_MODEL)
                # answer_complete 承载终检后的完整内容；空全文什么都不发——
                # 原先在此补裸 [DONE] 会让上层 _yielded 置真、fallback_chain 的
                # 空流兜底分支（answer_complete+[DONE]）永不触发，整条流反而
                # 缺成功事件（2026-09-20 审查 CHAT-3，与 :170 原注释承诺相反）
                if _safe_text:
                    yield sse("answer_complete", _safe_text) + "data: [DONE]\n\n"


def accumulate_sse_text(acc: str, raw_event: str) -> str:
    """从 fallback_chain 产出的 SSE 事件行聚合回答文本（供上层降级路径统一 finalize）。

    answer_complete 承载终检后的完整文本，直接覆盖累积值；answer_chunk 追加；
    [DONE]/解析失败行不影响累积。2026-09-14 审计 P1 配套：降级输出此前只转发
    不收尾，上层无法拿到最终文本做 finalize_answer。
    """
    payload = raw_event[5:].strip() if raw_event.startswith("data: ") else ""
    if not payload or payload == "[DONE]":
        return acc
    try:
        ev = jloads(payload)
    except Exception:
        return acc
    if not isinstance(ev, dict):
        return acc
    if ev.get("type") == "answer_complete" and ev.get("content"):
        return str(ev["content"])
    if ev.get("type") == "answer_chunk":
        return acc + str(ev.get("content") or "")
    return acc


async def fallback_chain(req: ChatRequest, username: str, cache_ctx: str = "") -> AsyncIterator[str]:
    """降级链：仅 Flash（Dify 已移除）

    兜底失败时写入短 TTL 空值占位防穿透风暴（await 而非 create_task，
    确保写入完成，P0 #11 防击穿占位不悬空）。
    """
    if is_civil_persona(getattr(req, "persona_id", "")):
        yield sse(
            "answer_complete",
            grounding_reply("service_error", getattr(req, "lang", "zh")),
        ) + "data: [DONE]\n\n"
        return
    yielded = False
    try:
        async for chunk in fallback_flash(req, username):
            yielded = True
            yield chunk
    except Exception:
        # P3 修复：保留 yielded 真值——Flash 已产出部分内容时不再叠加兜底文案（原实现
        # 重置 yielded 会造成"部分内容 + 兜底文案"先后发出）；补 [DONE] 终结事件防前端悬挂
        # （Flash 中途异常路径自身不发 [DONE]）
        if yielded:
            yield "data: [DONE]\n\n"
    # 兜底：主模型 + Flash 双双失败时，保证至少返回一个终结事件，避免空流（前端一直转圈）
    if not yielded:
        # 穿透防护：兜底失败 → 写入短 TTL 空值占位，避免后续请求持续打 LLM
        try:
            await SemanticCache.set_empty(req.query, cache_ctx=cache_ctx, ttl=30)
        except Exception as e:
            logger.debug(f"穿透占位写入失败（TTL 自愈）: {e}")
        yield sse("answer_complete", fallback_message(req)) + "data: [DONE]\n\n"

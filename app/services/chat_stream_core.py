"""流式编排核心：门禁组 + 意图路由 + 主流程 chat_generate

抽取自 app/routes/v2.py（2026-09 重构，行为等价纯移动）。
chat_generate 严格按原版顺序：
  civil_route_gate → serve_from_cache → rebuild_lock_gate → key → route_intent
  → law_mapping_gate → simple_fast_path → recommend_fast_path → react_loop
  → except 降级 fallback_chain；finally cleanup_stream。
"""
import asyncio
import time
from typing import AsyncIterator

from ..models.schemas import ChatRequest
from ..core.logging import setup_logging
from ..core.persona_manager import is_civil_persona
from ..core.semantic_cache import SemanticCache
from ..core.stream_utils import sse
from ..middleware.rate_limit import release_concurrent, renew_concurrent
from .chat_stream_ctx import ChatStreamCtx, build_stream_ctx, finalize_answer
from .chat_support import get_deepseek_key, thinking_text, safety_message
from .chat_fallback import fallback_chain
from .chat_fast_paths import simple_fast_path, recommend_fast_path
from .chat_react import react_loop
from .chat_travel_flow import (
    apply_travel_memory, build_travel_query, build_travel_state,
    next_missing_travel_slot, travel_clarification_gate,
)
from .rag_request_trace import (begin_request, finish_request, mark_first_token,
                                mark_route, record_span)

logger = setup_logging()

async def _tracked_stage(stage: str,
                         source: AsyncIterator[str]) -> AsyncIterator[str]:
    """记录阶段耗时与结果；生成器异常时保留失败状态。"""
    started = time.perf_counter()
    status = "ok"
    try:
        async for event in source:
            yield event
    except Exception:
        status = "failed"
        raise
    finally:
        record_span(
            stage,
            status=status,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )


def _busy_message(ctx: ChatStreamCtx) -> str:
    """返回当前语言的服务繁忙文案。"""
    if getattr(ctx.req, "lang", "zh") == "en":
        return "Service is busy, please try again later."
    return "服务暂时繁忙，请稍后重试。"


async def _refund_trial_if_unproduced(username: str) -> None:
    """SSE 侧试用额度归还（2026-09-20 审查 CHAT-1）。

    任务路径 09-14 决策 #4A 已对"未产生模型调用/输出"退还（runner.py
    _refund_quota_if_unproduced），SSE 侧此前只扣不退——繁忙/无 Key/降级
    空流三类终态用户没拿到真实回答也白扣一次额度，同一决策只落了一半。
    rollback 自带 quota_limited 条件，非受限用户是 no-op。"""
    from ..core.quota import rollback_used_questions
    try:
        await rollback_used_questions(username)
    except Exception as rb_err:
        logger.warning(f"试用额度归还失败（依赖故障面，用户可重试）: {rb_err}")


async def _finish_busy(ctx: ChatStreamCtx) -> AsyncIterator[str]:
    """防击穿锁不可用时收口，禁止在无锁状态下继续调用模型。"""
    message = _busy_message(ctx)
    yield sse("answer_chunk", message)
    yield sse("answer_complete", message) + "data: [DONE]\n\n"
    ctx.saved_normally = True
    ctx.finished = True
    await _refund_trial_if_unproduced(ctx.username)
    await finalize_answer(ctx, message, write_cache=False)


async def route_intent(ctx: ChatStreamCtx) -> None:
    """意图路由：先分类用户意图，只加载匹配工具；设置 ctx 的 agents/simple/recommend/tools"""
    from ..agents.router import classify_intent, get_tools_for_intent
    from ..core.config import INTENT_USE_LLM
    try:
        intent_result = await classify_intent(ctx.user_query, use_llm=INTENT_USE_LLM, username=ctx.username)
        if not is_civil_persona(ctx.persona_id):
            intent_result = await apply_travel_memory(ctx, intent_result)
        matched_agents = intent_result.get("agents", [])
        is_simple = intent_result.get("is_simple", True)
        is_recommend = intent_result.get("is_recommend", False)
        logger.info(f"意图路由: agents={matched_agents}, simple={is_simple}, recommend={is_recommend}")
        if is_civil_persona(ctx.persona_id):
            matched_agents = ["search_knowledge"]
            is_simple = True
            is_recommend = False
            logger.info("民法典人格，强制意图=search_knowledge")
        elif is_recommend:
            # 旅行规划是槽位填充：缺目的地/出发地时先澄清，禁止先烧四工具。
            state = await build_travel_state(ctx)
            missing_slot = next_missing_travel_slot(state)
            if missing_slot:
                state["missing_slot"] = missing_slot
                ctx.travel_pending = state
                ctx.travel_waiting_slot = True
                ctx.travel_pending_clear = False
            else:
                ctx.user_query = build_travel_query(state, getattr(ctx.req, "lang", "zh"))
        if matched_agents:
            tools = get_tools_for_intent(matched_agents)
        else:
            tools = []
    except Exception as route_err:
        logger.warning(f"意图路由异常（降级）: {route_err}")
        if is_civil_persona(ctx.persona_id):
            # civil 人格任何路径都不得降级为无工具通用回答：异常仍锁知识库检索
            from ..agents.tool_definitions import CIVIL_CODE_TOOLS
            matched_agents = ["search_knowledge"]
            is_simple, is_recommend, tools = True, False, CIVIL_CODE_TOOLS
        else:
            # 2026-09-25 修两链不对称：与任务链 _maybe_simple_task 异常口径统一为
            # "无工具纯 LLM"。原先赋 ALL_TOOLS 是死赋值（matched_agents=[] 走纯聊
            # 快速通道，不读 ctx.tools），一旦流形变化进 ReAct 就变成暗放全工具。
            matched_agents, is_simple, is_recommend, tools = [], True, False, []
    ctx.matched_agents = matched_agents
    ctx.is_simple = is_simple
    ctx.is_recommend = is_recommend
    ctx.tools = tools


async def civil_route_gate(ctx: ChatStreamCtx) -> AsyncIterator[str]:
    """前置路由裁决表（硬规则引擎）：civil 人格 adjudicate 领域，reject 直接返回"""
    if not is_civil_persona(getattr(ctx.req, 'persona_id', None)):
        return
    try:
        from ..agents.routing_table import route_query
        _route = route_query(ctx.req.query)
        ctx.civil_route_match = _route.match_type
        if _route.action == "reject":
            logger.info(f"路由裁决: reject → {_route.domain} ({_route.match_type})")
            mark_route("civil_reject")
            yield sse("answer_complete", _route.message) + "data: [DONE]\n\n"
            # 拒答也要收尾入历史：与 law_mapping_gate 同一标准，问答不丢上下文
            # write_cache=False：拒答是硬规则文案不是 LLM 成果，钉进 60 天缓存会让路由表
            # 修订在 TTL 内不生效（同问继续拿到旧拒答）
            await finalize_answer(ctx, _route.message, write_cache=False)
            ctx.saved_normally = True
            ctx.finished = True
            return
        if _route.action == "pass" and _route.mapped_query:
            ctx.civ_civil_mapped_query = _route.mapped_query
            logger.info(f"口语映射待用: {ctx.req.query[:30]}... → {_route.mapped_query[:60]}...")
    except Exception as _re:
        logger.warning(f"路由裁决异常（不影响正常流程）: {_re}")


async def serve_from_cache(ctx: ChatStreamCtx) -> AsyncIterator[str]:
    """语义缓存拦截（有文件上传时跳过）；命中/穿透占位直接返回"""
    if ctx.req.file_ids or ctx.has_history:
        return
    try:
        cached_response = await SemanticCache.get(ctx.req.query, cache_ctx=ctx.cache_ctx)
        if cached_response and await SemanticCache.is_empty(cached_response):
            logger.info("穿透占位命中，直接返回提示（不再打 LLM）")
            mark_route("cache_empty")
            _busy_msg = "服务暂时繁忙，请稍后重试。" if getattr(ctx.req, 'lang', 'zh') != 'en' else "Service is busy, please try again later."
            yield sse("answer_chunk", _busy_msg)
            yield sse("answer_complete", _busy_msg) + "data: [DONE]\n\n"
            # 2026-09-15 审查 I-07 残留：穿透占位提示也要 finalize——否则会话
            # 历史、日请求计数与 answer trace 缺失，与缓存命中分支（下述）
            # 同一口径。繁忙提示不入缓存（write_cache=False）。
            ctx.saved_normally = True
            await _refund_trial_if_unproduced(ctx.username)
            await finalize_answer(ctx, _busy_msg, write_cache=False)
            ctx.finished = True
            return
        if cached_response and not await SemanticCache.is_empty(cached_response):
            from ..core.safety_filter import get_filter
            sf = get_filter()
            if sf.contains_sensitive(cached_response):
                logger.warning("DFA 拦截语义缓存")
                cached_response = safety_message(ctx.req, sf.safe_message)
            logger.info("语义缓存命中")
            mark_route("cache_hit")
            yield sse("answer_chunk", cached_response)
            yield sse("answer_complete", cached_response) + "data: [DONE]\n\n"
            # 2026-09-12 修复（外部复核 P1）：缓存命中此前不调用 finalize_answer——
            # 会话历史缺失（下一轮追问失忆）且日请求计数缺失。缓存命中不计费但
            # 历史与请求计数必须与正常回答同口径；命中内容不入缓存（已是缓存）。
            # saved_normally 先于 finalize（C-F11 同款，2026-09-14 审查补）：
            # 内容已送达 [DONE]，finalize 异常不得触发上层二次降级
            ctx.saved_normally = True
            await finalize_answer(ctx, cached_response, write_cache=False)
            ctx.finished = True
            return
    except Exception as cache_err:
        logger.warning(f"缓存查询失败，继续正常推理: {cache_err}")


async def rebuild_lock_gate(ctx: ChatStreamCtx) -> AsyncIterator[str]:
    """防击穿（互斥重建）：无锁且缓存未命中时返回繁忙，禁止无锁调用模型。"""
    if ctx.req.file_ids or ctx.has_history:
        return
    try:
        ctx.rebuild_lock = await SemanticCache.acquire_rebuild_lock(ctx.req.query, cache_ctx=ctx.cache_ctx)
    except Exception as _rebuild_err:
        logger.warning(f"防击穿锁获取失败，返回繁忙: {_rebuild_err}")
        ctx.rebuild_lock = None
        async for ev in _finish_busy(ctx):
            yield ev
        return

    if ctx.rebuild_lock is None:
        await asyncio.sleep(0.3)
        try:
            _cached_retry = await SemanticCache.get(ctx.req.query, cache_ctx=ctx.cache_ctx)
            _cached_empty = bool(
                _cached_retry and await SemanticCache.is_empty(_cached_retry)
            )
        except Exception as _retry_err:
            logger.warning(f"防击穿锁等待后缓存检查失败，返回繁忙: {_retry_err}")
            async for ev in _finish_busy(ctx):
                yield ev
            return
        if _cached_empty:
            mark_route("cache_empty")
            async for ev in _finish_busy(ctx):
                yield ev
            return
        if _cached_retry:
            # 与 serve_from_cache 同一标准：缓存内容过 DFA 再返回。
            from ..core.safety_filter import get_filter
            _sf = get_filter()
            if _sf.contains_sensitive(_cached_retry):
                logger.warning("DFA 拦截语义缓存（重读路径）")
                _cached_retry = safety_message(ctx.req, _sf.safe_message)
            mark_route("cache_hit")
            yield sse("answer_chunk", _cached_retry)
            yield sse("answer_complete", _cached_retry) + "data: [DONE]\n\n"
            ctx.saved_normally = True
            await finalize_answer(ctx, _cached_retry, write_cache=False)
            ctx.finished = True
            return
        async for ev in _finish_busy(ctx):
            yield ev
        return

    if ctx.rebuild_lock:
        async def _renew_rebuild_loop() -> None:
            """后台续期防击穿锁：15s 一次；锁失效/被他人持有即停止"""
            while True:
                await asyncio.sleep(15)
                try:
                    if not await SemanticCache.renew_rebuild_lock(
                            ctx.req.query, ctx.rebuild_lock, cache_ctx=ctx.cache_ctx, ttl=45):
                        break
                except Exception:
                    break
        from ..core.concurrency import spawn as _spawn
        ctx.rebuild_renew_task = _spawn(_renew_rebuild_loop(), name="rebuild-renew")


async def law_mapping_gate(ctx: ChatStreamCtx) -> AsyncIterator[str]:
    """法律依据纠正映射表检查（优先于 LLM 调用；仅非强制白名单查询）"""
    if not is_civil_persona(ctx.persona_id):
        return
    # route_query 已由 civil_route_gate 执行并存入 ctx.civil_route_match，不再重复跑一遍
    if ctx.civil_route_match == "forced_whitelist":
        logger.info("法律映射表跳过：强制白名单命中")
        return
    try:
        from ..agents.law_mapping import check_query as _check_law
        _law_match = _check_law(ctx.user_query)
        if _law_match:
            logger.info(f"法律映射表命中 #{_law_match['id']}: {_law_match['scenario']} → {_law_match['law']}")
            mark_route("law_mapping")
            yield sse("answer_complete", _law_match['message']) + "data: [DONE]\n\n"
            # write_cache=False：映射表文案是规则成果不是 LLM 成果，钉进缓存会让
            # law_mapping 表修订在 TTL 内不生效（同问继续拿到旧引导语）
            await finalize_answer(ctx, _law_match['message'], write_cache=False)
            ctx.saved_normally = True
            ctx.finished = True
            logger.info("法律映射表已拦截，返回引导信息")
            return
    except Exception as _le:
        logger.warning(f"法律映射表检查失败（不影响正常流程）: {_le}")


async def cleanup_stream(ctx: ChatStreamCtx) -> None:
    """finally 清理：停止锁续期任务 → 释放防击穿锁 → 释放并发槽位 → 断线保存"""
    if ctx.rebuild_renew_task:
        ctx.rebuild_renew_task.cancel()
    if ctx.rebuild_lock:
        try:
            await SemanticCache.release_rebuild_lock(ctx.req.query, ctx.rebuild_lock, cache_ctx=ctx.cache_ctx)
        except Exception as _rel_err:
            logger.warning(f"防击穿锁释放失败（TTL 自愈兜底）: {_rel_err}")
    if ctx.concurrent_renew_task:
        ctx.concurrent_renew_task.cancel()
        try:
            await ctx.concurrent_renew_task
        except asyncio.CancelledError:  # noqa: silent-except 豁免：任务取消是预期收尾
            pass
    await release_concurrent(ctx.username, ctx.concurrent_lease)
    from ..core.config import EMPTY_ANSWER_VARIANTS
    if not ctx.saved_normally and ctx.mm and ctx.partial_answer and ctx.partial_answer not in EMPTY_ANSWER_VARIANTS:
        try:
            await ctx.mm.save_messages(
                {"role": "user", "content": ctx.req.query},
                {"role": "assistant", "content": ctx.partial_answer + "\n\n（内容不完整，连接已断开）"}
            )
            logger.info(f"断线保存: conv_id={ctx.conv_id}, 内容长度={len(ctx.partial_answer)}")
        except Exception as save_err:
            logger.warning(f"断线保存失败: {save_err}")


async def _prepare_chat_ctx(req: ChatRequest, current_user: dict,
                            today: str) -> ChatStreamCtx:
    """构建上下文并启动并发租约续期。"""
    ctx = await build_stream_ctx(req, current_user, today)
    if ctx.concurrent_lease and ctx.concurrent_lease != "__bypass__":
        async def _renew_lease_loop():
            while True:
                await asyncio.sleep(60)
                if not await renew_concurrent(ctx.username, ctx.concurrent_lease):
                    break
        from ..core.concurrency import spawn as _spawn
        ctx.concurrent_renew_task = _spawn(_renew_lease_loop(), name="lease-renew")
    return ctx


async def _run_chat_pipeline(ctx: ChatStreamCtx) -> AsyncIterator[str]:
    """按既有顺序执行门禁、缓存、路由、快速通道和 ReAct。"""
    async for ev in _tracked_stage("civil_route", civil_route_gate(ctx)):
        yield ev
        if ctx.finished:
            return
    async for ev in _tracked_stage("cache", serve_from_cache(ctx)):
        yield ev
        if ctx.finished:
            return
    async for ev in _tracked_stage("rebuild_lock", rebuild_lock_gate(ctx)):
        yield ev
        if ctx.finished:
            return
    if ctx.finished:
        return
    ctx.api_key = await get_deepseek_key()
    if not ctx.api_key:
        logger.error("DeepSeek API Key 未设置，无法调用")
        mark_route("config_error")
        record_span("generation", status="failed", error_code="missing_api_key")
        _config_error = "服务配置不完整（API Key 缺失），请联系管理员。"
        yield sse("answer_complete", _config_error) + "data: [DONE]\n\n"
        ctx.saved_normally = True
        ctx.finished = True
        # CHAT-1：无 Key 属"未产生模型调用"，与任务路径 runner.py:369 同口径退还
        await _refund_trial_if_unproduced(ctx.username)
        await finalize_answer(ctx, _config_error, write_cache=False)
        return
    _route_started = time.perf_counter()
    await route_intent(ctx)
    record_span(
        "intent_route",
        latency_ms=int((time.perf_counter() - _route_started) * 1000),
        attributes={"agents": ctx.matched_agents, "is_simple": ctx.is_simple,
                    "is_recommend": ctx.is_recommend},
    )
    async for ev in _tracked_stage("travel_clarification",
                                   travel_clarification_gate(ctx)):
        yield ev
        if ctx.finished:
            return
    if ctx.finished:
        return
    async for ev in _tracked_stage("law_mapping", law_mapping_gate(ctx)):
        yield ev
        if ctx.finished:
            return
    if (ctx.is_simple and len(ctx.matched_agents) == 1) or not ctx.matched_agents:
        async for ev in _tracked_stage("simple_fast_path", simple_fast_path(ctx)):
            yield ev
        if ctx.finished:
            return
    if ctx.is_recommend and len(ctx.matched_agents) >= 2:
        async for ev in _tracked_stage("recommend_fast_path", recommend_fast_path(ctx)):
            yield ev
        if ctx.finished:
            return
    async for ev in _tracked_stage("react", react_loop(ctx)):
        yield ev


async def _fallback_events(ctx: ChatStreamCtx | None,
                           error: Exception, lang: str = "zh") -> AsyncIterator[str]:
    """异常降级；已送达答案时不重复输出。降级输出聚合后统一 finalize。"""
    if ctx is not None and ctx.saved_normally:
        logger.warning(f"V2 收尾阶段异常（主回答已送达，跳过降级）: {error}")
        return
    if ctx is not None and is_civil_persona(ctx.persona_id):
        from .civil_grounding import grounding_reply
        _civil_error = grounding_reply("service_error", getattr(ctx.req, "lang", "zh"))
        logger.error(f"民法典链路异常，禁止通用知识降级: {error}")
        yield sse("answer_complete", _civil_error) + "data: [DONE]\n\n"
        ctx.saved_normally = True
        ctx.finished = True
        # CHAT-1：service_error 为固定文案未打模型，退还预留额度
        await _refund_trial_if_unproduced(ctx.username)
        await finalize_answer(ctx, _civil_error, write_cache=False)
        return
    logger.error(f"V2 未知异常，降级备胎: {error}", exc_info=True)
    mark_route("fallback")
    record_span("fallback", status="degraded", error_code=type(error).__name__)
    if ctx is None:
        # ctx 构建失败（如 Redis/PG 抖动）：没有 req 可喂降级链，
        # 直接给兜底文案，不再让降级链自身 AttributeError 变 500
        yield sse("answer_complete", thinking_text("service_unavailable", lang)) + "data: [DONE]\n\n"
        return
    from .chat_fallback import accumulate_sse_text
    _fb_text = ""
    try:
        async for chunk in fallback_chain(ctx.req, ctx.username, cache_ctx=ctx.cache_ctx):
            yield chunk
            _fb_text = accumulate_sse_text(_fb_text, chunk)
    except Exception as fb_err:
        logger.error(f"降级也失败: {fb_err}")
        _fb_text = _fb_text or thinking_text("service_unavailable", lang)
        yield sse("answer_complete", _fb_text) + "data: [DONE]\n\n"
    # 2026-09-14 审计 P1：顶层异常降级此前只转发 SSE 未 finalize——用户拿到
    # 答案但 trace 记失败、历史与日请求计数缺失。write_cache=False：降级答案
    # 可能残缺/为兜底文案，不入缓存。saved_normally 无条件置位：内容已送达，
    # finalize 结果只影响台账不影响用户
    if ctx is not None:
        ctx.saved_normally = True
        # CHAT-1：降级链零产出（_fb_text 为空=Flash 未吐出任何内容）时退还；
        # 有部分产出则按 #4A 口径"部分产出不退"
        if not _fb_text:
            await _refund_trial_if_unproduced(ctx.username)
        try:
            await finalize_answer(
                ctx, _fb_text or thinking_text("service_unavailable", lang), write_cache=False)
        except Exception as fin_err:
            logger.warning(f"降级收尾失败（内容已送达，不影响用户）: {fin_err}")


async def _cleanup_generation(ctx: ChatStreamCtx | None,
                              current_user: dict) -> None:
    """释放流式资源；ctx 构建失败时仍需释放并发槽位。"""
    if ctx is not None:
        await cleanup_stream(ctx)
    else:
        # CHAT-1：ctx 构建失败（Redis/PG 抖动）时额度已在准入处预留、必然零产出，退还
        await _refund_trial_if_unproduced(current_user["username"])
        await release_concurrent(
            current_user["username"], current_user.get("_concurrent_lease", "")
        )


async def _chat_generate_impl(req: ChatRequest, current_user: dict,
                              today: str) -> AsyncIterator[str]:
    """v2 流式对话主流程（SSE）；门禁/快速通道/ReAct 按原版顺序编排"""
    ctx = None
    try:
        ctx = await _prepare_chat_ctx(req, current_user, today)
        async for ev in _run_chat_pipeline(ctx):
            yield ev
    except Exception as e:
        async for ev in _fallback_events(ctx, e, getattr(req, "lang", "zh")):
            yield ev
    finally:
        await _cleanup_generation(ctx, current_user)


async def chat_generate(req: ChatRequest, current_user: dict,
                        today: str) -> AsyncIterator[str]:
    """为单次用户请求建立 trace 上下文并透传底层流。"""
    begin_request(
        conversation_id=getattr(req, "conversation_id", "") or "",
        username=current_user.get("username", ""),
    )
    emit_first = True
    try:
        async for event in _chat_generate_impl(req, current_user, today):
            if emit_first and (
                '"type": "answer_chunk"' in event
                or '"type": "answer_complete"' in event
            ):
                mark_first_token()
                emit_first = False
            yield event
    finally:
        finish_request(status="failed", error_code="stream_ended_without_finalize")

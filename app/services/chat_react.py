"""ReAct 循环：流式调用 / 工具并行执行 / 多 Agent 圆桌 / 消息回填 / max_steps 截断兜底

抽取自 app/routes/v2.py（2026-09 重构，行为等价纯移动）。
"""
import asyncio
import json
import uuid
from typing import AsyncIterator, Dict, List, Optional

from ..core.config import LLM_PRIMARY_MODEL, TOOL_TIMEOUT
from ..core.logging import setup_logging
from ..core.persona_manager import is_civil_persona
from ..core.stream_utils import dispatch_tool, sse
from ..core.safety_filter import (
    get_filter, sanitize_error_text, sanitize_untrusted_text,
    serialize_untrusted_value,
)
from .chat_stream_ctx import ChatStreamCtx, finalize_answer
from .chat_support import display_safe_tool_result, hide_reasoning, mark_key_result, thinking_text, safety_message, empty_answer, incomplete_answer
from .conversation_profiles import persist_weather_snapshot
from .llm_streaming import _spawn_drain_bill, record_token_usage, stream_llm_throttled
from .chat_fallback import fallback_chain
from .rag_request_trace import mark_route

logger = setup_logging()
MAX_STEPS = 3
# 圆桌讨论心跳间隔：远小于前端 60s 空闲看门狗（api/chat.ts），又不至于频繁发送
_HEARTBEAT_INTERVAL_S = 15.0


def _accumulate_tool_calls(tool_calls_index: Dict, delta: List[dict]) -> Dict:
    """合并同一 tool_call 的多个增量片段（id/name/arguments 按 index 拼装）"""
    for tc in delta:
        idx = tc.get("index")
        if idx is not None:
            if idx not in tool_calls_index:
                tool_calls_index[idx] = {
                    "id": tc.get("id", ""), "type": tc.get("type", "function"),
                    "function": {"name": "", "arguments": ""},
                }
            if tc.get("id"):
                tool_calls_index[idx]["id"] = tc["id"]
            if tc.get("function"):
                if tc["function"].get("name"):
                    tool_calls_index[idx]["function"]["name"] = tc["function"]["name"]
                if tc["function"].get("arguments"):
                    tool_calls_index[idx]["function"]["arguments"] += tc["function"]["arguments"]
    return tool_calls_index


def _frame_tool_calls(tool_calls_index: Dict) -> Optional[List[dict]]:
    """把 index 字典转成调用参数列表；无工具调用返回 None"""
    return [
        {
            "id": v["id"], "type": v["type"],
            "function": {"name": v["function"]["name"], "arguments": v["function"]["arguments"]},
        }
        for v in tool_calls_index.values()
    ]


def _drain_bill_blocked(agen, ctx: ChatStreamCtx, fallback_text: str,
                        bill_uid: str) -> None:
    """DFA 拦截 break 遗弃上游生成器时的计费排空（2026-09-12 深检 P1）。

    usage 尾块未消费则本步计 0，而上游 token 已真实消耗——与断连同款机制
    排空计费；正常结束的 usage 已在流内消费，不会双计。
    bill_uid 照断连同款传下去（P1-1）：此刻本步的 usage 可能已经扣过，排空的
    估算兜底必须落在同一个幂等键上。
    """
    _spawn_drain_bill(agen, ctx.username, ctx.conv_id, "react-blocked",
                      fallback_text=fallback_text, bill_uid=bill_uid,
                      file_ctx_chars=getattr(ctx, "file_ctx_chars", 0))


async def _fallback_and_finalize(ctx: ChatStreamCtx, full_content: str) -> AsyncIterator[str]:
    """降级链输出 + 统一收尾（2026-09-14 审计 P1）。

    降级输出此前只转发未 finalize——会话历史、日请求计数、RAG answer trace 全缺。
    聚合最终文本（answer_complete 承载终检后全文）后与正常路径同一收口；
    write_cache=False：降级答案可能残缺/为兜底文案，不入缓存。

    saved_normally 先于 finalize 置位（C-F11 同款教训）：降级内容此刻已送达前端，
    finalize 若抛异常绝不能让上层再降级出第二段回答。

    full_content 非空说明失败前已有 answer_chunk 送达前端，此时不得换模型重答
    （2026-09-19 审查 chat P2-1，与 llm_streaming.answer_via_models 同口径）。
    """
    if full_content:
        async for chunk in _finalize_partial_answer(ctx, full_content):
            yield chunk
        return
    from .chat_fallback import accumulate_sse_text
    _fb_text = ""
    async for chunk in fallback_chain(ctx.req, ctx.username, cache_ctx=ctx.cache_ctx):
        yield chunk
        _fb_text = accumulate_sse_text(_fb_text, chunk)
    _final = _fb_text or full_content or empty_answer(ctx.req)
    ctx.partial_answer = _final
    ctx.saved_normally = True
    ctx.finished = True
    try:
        await finalize_answer(ctx, _final, write_cache=False)
    except Exception as fin_err:
        logger.warning(f"降级收尾失败（内容已送达，不影响用户）: {fin_err}")


async def _finalize_partial_answer(ctx: ChatStreamCtx,
                                   full_content: str) -> AsyncIterator[str]:
    """已流出部分回答时的收口（2026-09-19 审查 chat P2-1）。

    换模型重答会与已送达的半截回答拼接成前后矛盾的两段，故只补完成标记；
    内容未经完整生成，不写语义缓存（write_cache=False）。
    """
    ctx.partial_answer = full_content
    yield sse("answer_complete", full_content) + "data: [DONE]\n\n"
    ctx.saved_normally = True
    ctx.finished = True
    try:
        await finalize_answer(ctx, full_content, write_cache=False)
    except Exception as fin_err:
        logger.warning(f"中途失败收尾失败（内容已送达，不影响用户）: {fin_err}")


async def _stream_react(ctx: ChatStreamCtx, model_try: str, frame: Dict) -> AsyncIterator[str]:
    """单步流式消费；产出 SSE；frame 记录 {full_reasoning, full_content, tool_calls, usage, fallback, content_blocked}"""
    full_reasoning = ""
    full_content = ""
    has_tool_calls = False
    content_blocked = False
    sf = get_filter()
    # 跨块敏感词守卫（2026-09-10 审查 P2）：块尾缓冲拼接后再送检，
    # 拦住被块边界切开的词；收尾终检仍保留作兜底
    from ..core.safety_filter import ContentStreamGuard
    _content_guard = ContentStreamGuard(sf)
    tool_calls_index: Dict = {}
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    # 本步上游调用的扣费幂等键（P1-1）：断连/DFA 拦截后的排空兜底沿用同一个
    _bill_uid = uuid.uuid4().hex
    _agen = stream_llm_throttled(
        ctx, ctx.api_key, model_try, ctx.messages,
        username=ctx.username, tools=ctx.tools, tool_choice="auto", bill_uid=_bill_uid,
    )
    try:
        async for kind, payload in _agen:
            if kind == "usage":
                usage.update(payload)
            elif kind == "reasoning":
                if not hide_reasoning(ctx.persona_id, ctx.persona):
                    # 思考已在 stream_llm_throttled 内经 ReasoningStreamGuard 行级过滤
                    # （勿再 sanitize：那会 strip 掉行尾换行，思考行在展示端粘连）
                    if payload:
                        full_reasoning += payload
                        yield sse("reasoning_chunk", payload)
            elif kind == "answer":
                # 逐块 DFA 过滤 + 跨块缓冲（2026-09-10）：answer_chunk 此刻已实时
                # 发往前端，事后检查拦不回（与 runner 任务路径同一标准）
                _piece, _blocked = _content_guard.feed(payload)
                if _blocked:
                    full_content += safety_message(ctx.req, sf.safe_message)
                    yield sse("answer_chunk", safety_message(ctx.req, sf.safe_message))
                    content_blocked = True
                    _drain_bill_blocked(_agen, ctx, full_content, _bill_uid)
                    break
                if _piece:
                    full_content += _piece
                    yield sse("answer_chunk", _piece)
            elif kind == "tool_calls":
                has_tool_calls = True
                _accumulate_tool_calls(tool_calls_index, payload["delta"])
        if not content_blocked:
            _tail, _tail_blocked = _content_guard.flush()
            if _tail_blocked:
                content_blocked = True
                full_content += safety_message(ctx.req, sf.safe_message)
                yield sse("answer_chunk", safety_message(ctx.req, sf.safe_message))
            elif _tail:
                full_content += _tail
                yield sse("answer_chunk", _tail)
        if full_reasoning:
            yield f"data: {json.dumps({'type': 'reasoning_done'})}\n\n"
    except Exception as e:
        logger.warning(f"DeepSeek API 调用失败 (流式): {e}")
        from ..core.metrics import llm_requests_total as _lrt
        _lrt.labels(model=model_try, endpoint='v2_chat', status='error').inc()  # 2026-09-11 审查 P1：告警接线
        await mark_key_result(ctx.api_key, False)
        # 中途失败收口（2026-09-19 审查 chat P2-1）：已流出部分回答时不再换模型重答，
        # 分派见 _fallback_and_finalize
        async for chunk in _fallback_and_finalize(ctx, full_content):
            yield chunk
        frame["fallback"] = True
        return
    except (GeneratorExit, asyncio.CancelledError):
        # 断连排空计费（09-09 P1）：上游流未关，排空拿真实 usage 补计费；
        # 不走 fallback（已断开），原样上抛不吞取消
        _spawn_drain_bill(_agen, ctx.username, ctx.conv_id, "react-step",
                          fallback_text=full_content, bill_uid=_bill_uid,
                          file_ctx_chars=getattr(ctx, "file_ctx_chars", 0))
        raise
    frame["full_reasoning"] = full_reasoning
    frame["full_content"] = full_content
    frame["content_blocked"] = content_blocked
    frame["usage"] = usage
    frame["tool_calls"] = _frame_tool_calls(tool_calls_index) if (has_tool_calls and tool_calls_index) else None


async def _tool_arg_error(func_name: str) -> dict:
    """LLM 工具参数不是合法 JSON 时的占位结果（对齐 runner._tool_arg_error，P1 修复）"""
    return {"error": f"{func_name} 参数不是有效 JSON"}


async def _execute_react_tools(ctx: ChatStreamCtx, tool_calls: List[dict], frame: Dict) -> AsyncIterator[str]:
    """并行执行工具（wait_for 超时手动 cancel）；产出事件；frame 记录 {tool_results, law_mapping_hit}"""
    # P1 修复：arguments 的 json.loads 必须有防护——LLM 偶发产出非法 JSON 时，
    # 原实现异常会打穿 async generator，整条 SSE 流无 answer_complete 直接断流
    parsed: List[tuple] = []
    for tc in tool_calls:
        func_name = tc["function"]["name"]
        try:
            args = json.loads(tc["function"]["arguments"])
        except (json.JSONDecodeError, TypeError):
            # 非法 JSON 也必须占位进 parsed：tool_results 与 tool_calls 按下标对齐，
            # 跳过会让坏调用拿到别人的结果、末尾 tool_call 缺 tool 消息（下轮 LLM 请求 400）
            args = None
        parsed.append((func_name, args))
        # args 与 tool_result 同过消毒：模型参数里带的是用户原句/检索词，
        # 直发等于绕过 DFA 的第二外发通道（10-07 审查 P2）
        yield f"data: {json.dumps({'type': 'tool_call', 'name': func_name, 'args': display_safe_tool_result(args) if args is not None else {}})}\n\n"

    tasks = []
    for func_name, args in parsed:
        if args is None:
            tasks.append(_tool_arg_error(func_name))
        else:
            # 兜底查询统一用改写后的 user_query（与 chat_fast_paths:175 同一口径，
            # 2026-09-07 审查 P2：原先一个用 req.query 一个用 user_query，语义不一致）
            _persona_id = getattr(ctx, "persona_id", "")
            if _persona_id:
                tasks.append(dispatch_tool(
                    func_name, args, ctx.user_query, ctx.user_perms, ctx.username,
                    persona_id=_persona_id,
                ))
            else:
                tasks.append(dispatch_tool(
                    func_name, args, ctx.user_query, ctx.user_perms, ctx.username,
                ))
    _tool_task_list = [asyncio.create_task(coro) for coro in tasks]
    try:
        tool_results = await asyncio.wait_for(
            asyncio.gather(*_tool_task_list, return_exceptions=True), timeout=TOOL_TIMEOUT)
    except asyncio.TimeoutError:
        for _t in _tool_task_list:
            if not _t.done():
                _t.cancel()
        logger.warning("工具并行调用超时，触发降级")
        _timeout_note = thinking_text(
            "kb_timeout_civil" if is_civil_persona(ctx.persona_id) else "kb_timeout",
            ctx.req,
        )
        yield sse("reasoning_chunk", _timeout_note)
        tool_results = [{"error": "工具查询超时", "fallback": True} for _ in tool_calls]

    for idx, result in enumerate(tool_results):
        if isinstance(result, Exception):
            # CHAT-4（2026-09-20 审查）：异常 str 可含带 key 的完整 URL；对外事件
            # 早已过消毒，进 LLM 上下文的这份拷贝此前是裸的（两份口径不一致）
            result = {"error": sanitize_error_text(str(result))}
        func_name = (
            tool_calls[idx]["function"]["name"]
            if idx < len(tool_calls) else ""
        )
        if func_name == "query_weather":
            await persist_weather_snapshot(getattr(ctx, "mm", None), result)
        # 对外事件消毒（2026-09-10 审查 P2）：第三方内容/错误串不直发前端；
        # frame["tool_results"] 仍持原对象供 LLM 上下文与圆桌使用
        yield f"data: {json.dumps({'type': 'tool_result', 'name': func_name, 'index': idx, 'result': display_safe_tool_result(result)})}\n\n"
    law_mapping_hit = None
    for _res in tool_results:
        if isinstance(_res, dict) and _res.get("mapping_hit"):
            law_mapping_hit = _res.get("message", "")
            break
    frame["tool_results"] = tool_results
    frame["law_mapping_hit"] = law_mapping_hit


def _build_tool_messages(tool_calls: List[dict], tool_results: List[dict]) -> List[dict]:
    """把工具结果拼装为 tool 角色消息（error → 友好降级文案）。

    2026-09-12 修复（外部复核 P1）：回喂模型的内容须经 _sanitize_context——
    web_search 等第三方结果可携带"忽略之前指令"式提示词注入（react_steps
    同款修复，本函数此前漏接）。
    """
    tool_messages = []
    for idx, result in enumerate(tool_results):
        if isinstance(result, Exception):
            # CHAT-4：同上方 tool_result 事件口径，异常 str 剥 URL/Key 后再进上下文
            result = {"error": sanitize_error_text(str(result))}
        if isinstance(result, dict) and "error" in result:
            error_msg = result["error"]
            fallback_text = "获取数据失败了，可能服务暂时不可用。"
            if "未找到" in error_msg:
                fallback_text = "暂时没找到这个城市的数据，建议换个关键词试试"
            elif "超时" in error_msg:
                fallback_text = "查询有点慢，可能网络问题，请稍后再试"
            elif "Key" in error_msg or "授权" in error_msg:
                fallback_text = "服务配置正在更新，暂时无法使用，我试试其他方式帮你。"
            result = {"error": error_msg, "fallback_message": fallback_text, "success": False}
        tool_messages.append({
            "role": "tool", "tool_call_id": tool_calls[idx]["id"],
            "content": serialize_untrusted_value(result),
        })
    return tool_messages


def _build_assistant_message(tool_calls: List[dict], full_content: str) -> dict:
    """构造 assistant 消息（含 tool_calls），供回填对话历史"""
    return {
        "role": "assistant", "content": full_content,
        "tool_calls": [
            {
                "id": v["id"], "type": v["type"],
                "function": {"name": v["function"]["name"], "arguments": v["function"]["arguments"]},
            }
            for v in tool_calls
        ],
    }


async def _roundtable(ctx: ChatStreamCtx, tool_calls: List[dict], tool_results: List[dict], frame: Dict) -> AsyncIterator[str]:
    """多 Agent 圆桌讨论；产出 thought 事件；frame 记录 {discussion_summary}"""
    discussion_summary = ""
    try:
        from ..agents.orchestrator import AgentOrchestrator
        from ..agents.router import get_agent_names_for_orchestrator
        # username 透传（2026-09-14 审计 P1）：圆桌 Phase1 的 LLM 调用原先全部
        # 漏计费——AgentOrchestrator 按 username 归户计量
        orch = AgentOrchestrator(ctx.req.query, username=ctx.username)
        agent_names = get_agent_names_for_orchestrator(ctx.matched_agents)
        if not agent_names:
            agent_names = ["query_weather", "query_hotel", "query_route", "query_food"]
        for idx, tc in enumerate(tool_calls):
            func_name = tc["function"]["name"]
            result = tool_results[idx] if idx < len(tool_results) else {"error": "missing result"}
            if isinstance(result, Exception):
                result = {"error": str(result)}
            if func_name in agent_names:
                if isinstance(result, dict) and "error" in result:
                    orch.add_tool_result(
                        func_name, {},
                        error=sanitize_untrusted_text(str(result["error"])),
                    )
                else:
                    orch.add_tool_result(
                        func_name,
                        {"untrusted_tool_data": serialize_untrusted_value(result)},
                    )
        if orch.get_involved_agents() and len(agent_names) > 1:
            yield sse("thought", thinking_text("experts", ctx.req))
            from ..core.config import ORCH_ENABLE_PHASE2
            # 圆桌 Phase1 并行多次子 Agent LLM 调用，期间零 SSE 外发——前端
            # 60s 空闲看门狗（api/chat.ts）会把这段静默误判成连接僵死而 cancel，
            # 表现为"卡在调用完成工具后"。把讨论跑成任务，等待期间每
            # _HEARTBEAT_INTERVAL_S 秒发一条 SSE 注释行（解析器忽略、字节到达即续命）。
            _run = asyncio.ensure_future(
                orch.run(enable_phase2=ORCH_ENABLE_PHASE2))
            while True:
                try:
                    discussion_summary = await asyncio.wait_for(
                        asyncio.shield(_run), timeout=_HEARTBEAT_INTERVAL_S)
                    break
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
    except Exception as orch_err:
        logger.warning(f"多Agent讨论异常（降级为常规模式）: {orch_err}")
    frame["discussion_summary"] = discussion_summary


async def _inject_discussion(ctx: ChatStreamCtx, discussion_summary: str) -> AsyncIterator[str]:
    """把讨论摘要发到思考区并作为内部参考注入 system（禁止提及"专家/讨论"等词）

    CHAT-1（2026-09-19 审查）：摘要出自子 Agent 的 LLM 输出，与主链路思考同
    性质，必须过同一个 ReasoningStreamGuard——此前模型身份/端点/工具失败类
    内容可整段零过滤外泄。

    批1 B-P1-2（2026-09-28）：给模型的 system 副本与给前端的思考副本**必须同源**。
    改前只过滤了外发那一份，注入 prompt 的仍是裸摘要——守卫挡掉的那些内容照样以
    "内部参考"的身份进了上下文，主模型把它复述进回答就绕过了整个 CHAT-1 闸门。
    """
    if not discussion_summary:
        return
    from .reasoning_guard import ReasoningStreamGuard
    guard = ReasoningStreamGuard(getattr(ctx, "persona_id", ""))
    safe_text = guard.feed(discussion_summary) + guard.flush()
    for line in safe_text.split('\n'):
        if line.strip():
            yield sse("reasoning_chunk", line + "\n")
    yield f"data: {json.dumps({'type': 'reasoning_done'})}\n\n"
    ctx.messages.append({
        "role": "system",
        "content": (
            "以下是基于工具查询结果的专业分析，已作为内部参考提供给你。\n"
            "请**直接**使用这些信息来回答用户，形成一份完整、连贯、自然的回答。\n"
            "**禁止在回答中提及**「专家」「天气专家说」「据讨论」「根据分析」等词汇。\n"
            "不要引用任何专家意见，不要用「某某专家认为」的句式。\n"
            "用你自己的口吻，把这些信息整合成一段流畅的建议。\n\n"
            f"{safe_text}"
        ),
    })


async def _react_step(ctx: ChatStreamCtx, step: int) -> AsyncIterator[str]:
    """一步 ReAct：流式 → 若纯文本则收尾；否则工具执行/圆桌/回填后进入下一步"""
    frame: Dict = {}
    # 跟人格选定模型（10-08 台账 469）：缺省回落主力槽。步内不换模型重试——
    # 本步的 answer_chunk 已实时外发，换模型重答会与半截重复（D2 P2-1 契约）
    _model_used = getattr(ctx, "selected_model", "") or LLM_PRIMARY_MODEL
    async for ev in _stream_react(ctx, _model_used, frame):
        yield ev
    if frame.get("fallback"):
        return
    # L2：文件注入长度并入 usage 携带（同会话只计一次的扣除依据）
    record_token_usage(
        {**frame["usage"], "file_ctx_chars": getattr(ctx, "file_ctx_chars", 0)},
        ctx.username, ctx.conv_id, model=_model_used)

    # 流中被 DFA 拦截：直接以已发送的安全文本收尾（full_content = 安全前缀 + 安全文案），
    # 不再进入工具分支；被拦截内容不写缓存（避免把"安全文案"钉成该 query 的长期答案）
    if frame.get("content_blocked"):
        final_safe = frame["full_content"] or empty_answer(ctx.req)
        yield sse("answer_complete", final_safe) + "data: [DONE]\n\n"
        ctx.partial_answer = final_safe
        # 先置位再 finalize（C-F11，与 fast_paths 09-28 统一口径）：内容已送达，
        # 台账异常绝不能让上层再降级出第二段回答
        ctx.saved_normally = True
        ctx.finished = True
        await finalize_answer(ctx, final_safe, write_cache=False)
        return

    tool_calls = frame["tool_calls"]
    if not tool_calls:
        # 纯文本回答：内容已逐块流式发送，此处仅做 DFA 检查 + 完成标记
        _cacheable = bool(frame["full_content"])
        final_safe = frame["full_content"] or empty_answer(ctx.req)
        sf = get_filter()
        if sf.contains_sensitive(final_safe):
            logger.warning(f"DFA 拦截响应")
            final_safe = safety_message(ctx.req, sf.safe_message)
            _cacheable = False
            yield sse("answer_chunk", final_safe)
        yield sse("answer_complete", final_safe) + "data: [DONE]\n\n"
        ctx.partial_answer = final_safe
        ctx.saved_normally = True  # 先置位再 finalize（C-F11）
        ctx.finished = True
        await finalize_answer(ctx, final_safe, write_cache=_cacheable)
        return

    async for ev in _execute_react_tools(ctx, tool_calls, frame):
        yield ev
    tool_results, law_mapping_hit = frame.get("tool_results"), frame.get("law_mapping_hit")
    if law_mapping_hit:
        logger.info("法律映射表命中，跳过LLM生成，直接返回引导信息")
        yield sse("answer_complete", law_mapping_hit) + "data: [DONE]\n\n"
        # write_cache=False：与 law_mapping_gate 同口径——规则文案不是 LLM 成果，
        # 进缓存会让映射表修订在 TTL 内不生效
        ctx.saved_normally = True  # 先置位再 finalize（C-F11）
        ctx.finished = True
        await finalize_answer(ctx, law_mapping_hit, write_cache=False)
        return

    if is_civil_persona(ctx.persona_id):
        from .civil_grounding import civil_tool_result, grounding_reply
        if not any(civil_tool_result(result) for result in (tool_results or [])):
            _civil_stop = grounding_reply("no_evidence", ctx.req.lang)
            yield sse("answer_complete", _civil_stop) + "data: [DONE]\n\n"
            ctx.saved_normally = True  # 先置位再 finalize（C-F11）
            ctx.finished = True
            await finalize_answer(ctx, _civil_stop, write_cache=False)
            return

    round_frame: Dict = {}
    async for ev in _roundtable(ctx, tool_calls, tool_results, round_frame):
        yield ev
    discussion_summary = round_frame.get("discussion_summary", "")

    ctx.messages.append(_build_assistant_message(tool_calls, frame["full_content"]))
    ctx.messages.extend(_build_tool_messages(tool_calls, tool_results))
    async for ev in _inject_discussion(ctx, discussion_summary):
        yield ev


async def react_loop(ctx: ChatStreamCtx) -> AsyncIterator[str]:
    """ReAct 主循环（max_steps）；耗尽后返回已有内容兜底"""
    mark_route("react")
    yield sse("thought", thinking_text("analyzing", ctx.req))
    await ctx.mm.save_user_message({"role": "user", "content": ctx.req.query})
    # P0 修复（2026-09-11 规则审查）：ctx.messages 由 _assemble_messages 组装时
    # 只有 system+历史（旧版 v2.py 单体里有 user 消息追加，纯移动重构时丢失），
    # 导致 ReAct 路径的 LLM 在"没有当前问题"的上下文上作答/决定工具调用。
    # 此处恰好每请求进入一次，且 fast path 不经过本函数（自行追加），互不重复。
    ctx.messages.append({"role": "user", "content": ctx.user_query})
    for step in range(MAX_STEPS):
        async for ev in _react_step(ctx, step):
            yield ev
        if ctx.finished:
            return
    # 循环结束未返回（max_steps 耗尽）
    if not ctx.saved_normally:
        last_content = ""
        for m in reversed(ctx.messages):
            if m.get("role") == "assistant" and m.get("content"):
                last_content = m["content"]
                break
        if not last_content:
            if is_civil_persona(ctx.persona_id):
                from .civil_grounding import grounding_reply
                last_content = grounding_reply("service_error", ctx.req.lang)
            else:
                last_content = incomplete_answer(ctx.req)
        logger.warning(f"V2 循环达到最大步数，返回已有内容: {len(last_content)} chars")
        yield sse("answer_complete", last_content) + "data: [DONE]\n\n"
        ctx.partial_answer = last_content
        ctx.saved_normally = True  # 先置位再 finalize（C-F11）
        ctx.finished = True
        await finalize_answer(ctx, last_content, write_cache=False)

"""快速通道：单 Agent 简单任务 + 推荐多 Agent（已知意图直接并行工具 + 单次 LLM 格式化）

抽取自 app/routes/v2.py（2026-09 重构，行为等价纯移动）。
含 as_completed 返回值回查修复（P0 #45）：原实现用 as_completed 产出的包装协程
回查 {Task: agent} 字典在 Python 3.11 必 KeyError，整条推荐通道从未真正跑通、
总静默降级到 ReAct；现改由每个子任务自行返回 (agent, result)。
"""
import asyncio
import json
import time
from datetime import datetime
from typing import AsyncIterator, Dict, List, Tuple

from ..core.config import TOOL_TIMEOUT
from ..core.logging import setup_logging
from ..core.constants import today_cn
from ..core.persona_manager import get_persona_manager, is_civil_persona
from ..core.place_extract import auto_tool_args
from ..core.safety_filter import (
    ContentStreamGuard, format_untrusted_tool_context, get_filter, sanitize_error_text,
)
from ..core.stream_utils import dispatch_tool, sse  # build_file_context 已移除（F1：文件上下文由 ctx.messages 承载，勿再拼）
from .chat_stream_ctx import ChatStreamCtx, finalize_answer
from .chat_react import _finalize_partial_answer  # 半截外发收口与 ReAct 同一口径（10-07 chat P2-1 旁路）
from .chat_support import (
    display_safe_tool_result, hide_reasoning, lang_instruction, safe_format_prompt,
    thinking_text, safety_message,
)
from .conversation_profiles import persist_weather_snapshot
from .civil_grounding import (
    civil_tool_result,
    find_ungrounded_citations as _find_ungrounded_citations,
    grounding_reply,
    validate_civil_answer,
)
from .llm_streaming import answer_via_models, ensure_answer, apply_safety_filter
from .rag_request_trace import mark_route, record_span

logger = setup_logging()

# 推荐通道仅旅游四工具参与（与原行为一致）
RECOMMEND_TOUR_TOOLS = ("query_weather", "query_hotel", "query_route", "query_food")

# 失败工具 → 用户可感知的板块名：注入话术点名缺失板块而非报数字（2026-09-10 实测：
# "有4项未能获取"会被思考模型原样复述并困惑"不知道哪些未能获取"；板块名是用户
# 可感知词，复述无害，也给了模型可行动信息）
_MISSING_SECTION_ZH = {
    "query_weather": "天气", "query_hotel": "住宿",
    "query_route": "交通", "query_food": "美食",
}


def find_ungrounded_citations(answer: str, contexts: List[str]) -> List[str]:
    """兼容旧调用点：引用溯源校验已下沉到 civil_grounding 共用实现。"""
    return _find_ungrounded_citations(answer, contexts)


def _record_ungrounded_citations(result_text: str, tool_result) -> None:
    """记录检索回答中的无依据条文号；监测失败不影响回答主流程。"""
    if not (isinstance(tool_result, dict) and tool_result.get("results")):
        return
    contexts = [
        f"{item.get('heading', '')}\n{item.get('content', '')}"
        for item in tool_result["results"]
    ]
    ungrounded = find_ungrounded_citations(result_text, contexts)
    if ungrounded:
        logger.warning(f"引用校验: 回答引用了检索依据之外的条文号: {ungrounded}")
    try:
        from .answer_trace import set_ungrounded_citations
        set_ungrounded_citations(ungrounded)
    except Exception:  # noqa: silent-except — 监测失败不影响回答
        pass


# 输出纪律（防泄漏，两通道共用）：错误/矛盾的工具数据曾诱发模型整段元推理并漏进正文
# （线上案例：注入出发地天气+error 后，模型在正文里纠结"为什么查的是广州"）
_ANSWER_DISCIPLINE = (
    "### 输出纪律（最高优先级）\n"
    "1. 直接输出给用户看的正式回答；禁止输出内心独白、自我质疑、对数据的元评论"
    "（如「为什么查的是某城市」「这是不是错误数据」「需要调用的工具」）\n"
    "2. 禁止出现工具名、函数名、JSON 键名等内部细节\n"
    "3. 数据与用户问的目的地不符时忽略该数据，按用户真正问的目的地回答\n"
    "4. 不要向用户提到查询失败、技术故障或数据缺失——相关内容自然跳过即可\n"
)


def build_simple_tool_args(agent_name: str, ctx: ChatStreamCtx) -> Tuple[Dict, str]:
    """构造简单任务工具参数，返回 (args, 状态提示文案)

    参数语义统一走 place_extract（目的地=问题中的城市；出发地只作 route 起点），
    与推荐通道/router 同源；本函数仅负责各工具的状态文案。
    """
    if agent_name == "search_knowledge":
        q = ctx.civ_civil_mapped_query or ctx.user_query
        logger.info(f"search_knowledge 查询: {q[:80]}... (原: {ctx.user_query[:30]}...)")
        return {"query": q}, thinking_text("kb", ctx.req)
    args = auto_tool_args(agent_name, ctx.user_query)
    if agent_name == "query_weather":
        return args, thinking_text("weather", ctx.req, city=args.get('city', ''))
    if agent_name == "query_hotel":
        return args, thinking_text("hotel", ctx.req, destination=args.get('destination', ''))
    if agent_name == "query_food":
        return args, thinking_text("food", ctx.req, destination=args.get('destination', ''))
    if agent_name == "query_route":
        return args, thinking_text(
            "route", ctx.req,
            departure=args.get('departure', ''), destination=args.get('destination', ''),
        )
    return args, thinking_text("search_info", ctx.req)


def _fast_system_base(ctx: ChatStreamCtx) -> str:
    """快速通道共享的 system 前缀（人格 + 语言指令 + 城市 + civil 路由结论）"""
    _pm = get_persona_manager()
    # 按请求解析人格：ctx 已解析的优先，否则按 req.persona_id；不改全局 current
    _persona = ctx.persona or _pm.get_effective(getattr(ctx.req, "persona_id", None))
    _today_str = today_cn()
    _system = (
        safe_format_prompt(_persona.system_prompt, today=_today_str, name=_persona.name)
        if _persona else f"你是AI助手。今天是{_today_str}。"
    )
    _system += ctx.lang_instr
    if is_civil_persona(ctx.persona_id):
        _system += (
            f"\n\n### 路由裁决：此问题已被确认为民法典问题\n"
            f"即使该问题也涉及其他法律领域，你**必须**先回答《民法典》中的相关规定（引用具体法条），\n"
            f"然后才可简要提及也涉及其他法律。**不得**以「不属于民法典」为由拒绝回答。\n"
            f"这是最高优先级指令。"
        )
    return _system


def _messages_for_fast_path(ctx: ChatStreamCtx, system_content: str) -> list:
    """复用共享历史：替换 system，保留文件上下文/历史，再追加当前问题。"""
    messages = [{"role": "system", "content": system_content}]
    if ctx.messages:
        messages.extend(ctx.messages[1:])
    messages.append({"role": "user", "content": ctx.user_query})
    return messages


async def _build_fast_system(ctx: ChatStreamCtx, agent_name: str, tool_result) -> str:
    """简单任务：在 system_base 上追加工具结果/无数据引导

    文件上下文由 ctx.messages[1:] 原样承载（_assemble_messages 已注入），此处
    **不得重复拼接**（2026-09-28 chat 链审查 F1 修复）：原先再调一次
    build_file_context 拼进 system，6 万字文件在 prompt 里出现两份——token
    双倍计费，且 ctx.file_ctx_chars（L2 同会话只计一次的扣除依据）只记一份
    长度，钱包口径与实际注入不符。
    """
    _system = _fast_system_base(ctx)
    if tool_result is not None:
        _has_data = False
        if isinstance(tool_result, dict):
            _has_data = bool(tool_result.get('results')) or bool(tool_result.get('temperature')) or bool(tool_result.get('city'))
        elif isinstance(tool_result, str) and len(tool_result) > 10:
            _has_data = True
        if _has_data:
            _system += (
                f"\n\n你使用工具 [{agent_name}] 查询到了以下结果。\n\n"
                f"## 核心规则\n"
                f"1. **禁止编造**：只能基于工具返回的数据回答，不能编造任何具体数据\n"
                f"2. **先推荐再追问**：如果用户是求推荐，第一句就直接给推荐方案\n"
            )
            if is_civil_persona(ctx.persona_id):
                # 法条类规则仅对民法典人格有意义，注入旅游答案只会造成干扰
                _system += (
                    f"3. **只答最相关依据**：从检索结果中选择与用户问题最直接相关的一条或少数法条，"
                    f"不得为了面面俱到而堆砌全部相关法条或章节\n"
                    f"4. **仅限民法典依据**：不得使用模型自身知识、常识、联网信息或其他法律条文补全回答；"
                    f"检索结果不足时明确说明无法依据知识库回答\n"
                    f"5. **禁止「让我先查询」**：第一句话必须是法条引用或直接答案\n"
                )
            _system += (
                f"3. **输出纪律**：直接输出给用户看的正式回答；禁止内心独白、自我质疑、"
                f"对数据的元评论，禁止出现工具名、JSON 键名等内部细节；"
                f"数据与用户问的目的地不符时忽略该数据\n"
                f"\n工具返回的数据：\n{format_untrusted_tool_context(tool_result)}"
            )
            if agent_name == "search_knowledge":
                # 引用溯源：条文号绑定检索结果，末尾输出依据清单（配合 find_ungrounded_citations 校验）
                _system += (
                    "\n### 引用溯源要求\n"
                    "1. 引用法条时，条文号必须取自上面检索结果中的条文号\n"
                    "2. 回答末尾另起一行，以「依据:」开头，列出本次回答实际引用的条文号\n"
                    "3. 禁止引用检索结果中不存在的条文号\n"
                    "4. 只回答与问题最直接相关的法条，不要求覆盖所有关联法条"
                )
        else:
            if is_civil_persona(ctx.persona_id):
                # 民法典人格零召回分支：只允许确定性拒答，不得靠模型知识补全。
                _system += (
                    f"\n\n工具 [{agent_name}] 未找到相关知识库中的对应内容。\n"
                    f"## 规则（最高优先级）\n"
                    f"1. **第一句话明确告知**：知识库中未检索到与该问题直接对应的条文\n"
                    f"2. **不得提供法律结论**：不得使用自身知识、一般法律常识、联网信息或其他法律条文补全\n"
                    f"3. **禁止引用任何条文号**（如「第X条」）\n"
                    f"4. **绝对禁止**说「让我先查询」「我来查一下」「请稍等」「正在查询」等任何等待语\n"
                    f"5. 只提示用户补充事实或换一种问法后重试"
                )
            else:
                _system += (
                    f"\n\n工具 [{agent_name}] 本次未返回有效数据，请基于你自己的知识直接回答用户的问题。\n"
                    f"## 规则（最高优先级）\n"
                    f"1. **第一句话就直接给答案/方案**，禁止任何铺垫\n"
                    f"2. **绝对禁止**说「让我先查询」「我来查一下」「请稍等」「正在查询」等任何等待语\n"
                    f"3. 涉及实时数据（价格/班次/天气数值）不要编造具体数字\n"
                    f"4. **不要**向用户提到查询失败、技术故障或数据缺失，自然跳过即可\n"
                )
    return _system


async def _finish_simple_terminal(ctx: ChatStreamCtx, text: str,
                                  write_cache: bool = False) -> AsyncIterator[str]:
    """输出并收尾快速通道结果，供民法典确定性兜底复用。"""
    yield sse("answer_chunk", text)
    yield sse("answer_complete", text) + "data: [DONE]\n\n"
    # C-F11 契约（F2 顺序统一，2026-09-28）：[DONE] 已送达，先置位再 finalize——
    # 即便未来 finalize 不再整体吞异常，也不会被上层当未送达而二次降级
    ctx.saved_normally = True
    ctx.finished = True
    await finalize_answer(ctx, text, write_cache=write_cache)


def _civil_stop_message(ctx: ChatStreamCtx, agent_name: str, tool_result) -> str | None:
    """民法典快速通道的零召回/跨法域收口文案；非民法典返回 None。"""
    if not is_civil_persona(ctx.persona_id) or agent_name != "search_knowledge":
        return None
    if isinstance(tool_result, dict) and tool_result.get("mapping_hit"):
        return tool_result.get("message") or grounding_reply("no_evidence", ctx.req.lang)
    if civil_tool_result(tool_result) is None:
        return grounding_reply("no_evidence", ctx.req.lang)
    return None


async def _stream_fast_model(ctx: ChatStreamCtx, messages: list,
                             civil_buffer: bool, state: dict) -> AsyncIterator[str]:
    """消费快速通道模型流；民法典缓冲到校验后再外发，其他人格保持实时流。"""
    sf = get_filter()
    guard = ContentStreamGuard(sf)
    state.update({"text": "", "generated": False, "interrupted": False})
    started = time.perf_counter()
    reasoning_chars = 0
    async for kind, text in answer_via_models(ctx, messages, "简单任务"):
        if kind == "reasoning":
            reasoning_chars += len(text or "")
            # 思考外发门禁与 ReAct 路径同口径（2026-09-19 审查 chat P2-2）：
            # 指标照常计数，只拦外发
            if not hide_reasoning(ctx.persona_id, ctx.persona) and text:
                yield sse("reasoning_chunk", text)
        elif kind == "answer":
            piece, blocked = guard.feed(text)
            if blocked:
                state["text"] += safety_message(ctx.req, sf.safe_message)
                state["interrupted"] = True
                yield sse("answer_chunk", safety_message(ctx.req, sf.safe_message))
                break
            if piece:
                state["generated"] = True
                state["text"] += piece
                if not civil_buffer:
                    yield sse("answer_chunk", piece)
    if not state["interrupted"]:
        tail, tail_blocked = guard.flush()
        if tail_blocked:
            state["text"] = safety_message(ctx.req, sf.safe_message)
            state["interrupted"] = True
            yield sse("answer_chunk", safety_message(ctx.req, sf.safe_message))
        elif tail:
            state["generated"] = True
            state["text"] += tail
            if not civil_buffer:
                yield sse("answer_chunk", tail)
    state["sf"] = sf
    record_span(
        "generation",
        latency_ms=round((time.perf_counter() - started) * 1000),
        attributes={
            # 与下方 getattr(ctx, "username", "") 同口径：ctx 由调用方构造，
            # 缺字段只影响埋点标签，不该打断快速通道（2026-09-16）。
            "model": getattr(ctx, "selected_model", ""),
            "reasoning_chars": reasoning_chars,
            "answer_chars": len(state["text"]),
        },
    )


async def simple_fast_path(ctx: ChatStreamCtx) -> AsyncIterator[str]:
    """单 Agent 或纯 LLM：直接执行工具(如有) + 单次 LLM 格式化"""
    is_pure_chat = len(ctx.matched_agents) == 0
    agent_name = ctx.matched_agents[0] if not is_pure_chat else "chat"
    mark_route("simple_fast_path")
    logger.info(f"简单任务快速通道: agent={agent_name}")
    yield sse("thought", thinking_text("search_info" if not is_pure_chat else "thinking", ctx.req))
    state: dict = {}  # 声明在 try 外：except 分支要能读到"已外发了多少"（10-07 chat P2-1 旁路）
    try:
        tool_result = None
        args = {}
        if not is_pure_chat:
            args, _status = build_simple_tool_args(agent_name, ctx)
            yield sse("reasoning_chunk", _status)
            tool_result = await dispatch_tool(
                agent_name, args, ctx.user_query, ctx.user_perms,
                getattr(ctx, "username", ""), persona_id=ctx.persona_id,
            )
            if agent_name == "query_weather":
                await persist_weather_snapshot(getattr(ctx, "mm", None), tool_result)
            if agent_name == "search_knowledge":
                from ..core.audit import audit
                await audit(ctx.username, "kb_search", {"query": ctx.user_query[:200]})
            yield f"data: {json.dumps({'type': 'reasoning_done'})}\n\n"
            yield f"data: {json.dumps({'type': 'tool_call', 'name': agent_name, 'args': display_safe_tool_result(args)})}\n\n"
            yield f"data: {json.dumps({'type': 'tool_result', 'name': agent_name, 'index': 0, 'result': display_safe_tool_result(tool_result)})}\n\n"

        civil_buffer = is_civil_persona(ctx.persona_id) and agent_name == "search_knowledge"
        stop_message = _civil_stop_message(ctx, agent_name, tool_result)
        if stop_message:
            async for event in _finish_simple_terminal(ctx, stop_message):
                yield event
            return

        system_fast = await _build_fast_system(ctx, agent_name, tool_result)
        messages = _messages_for_fast_path(ctx, system_fast)
        async for event in _stream_fast_model(ctx, messages, civil_buffer, state):
            yield event
        result_text = state["text"]
        civil_grounded = True
        if civil_buffer and not state["interrupted"]:
            if not result_text:
                result_text = grounding_reply("service_error", ctx.req.lang)
                civil_grounded = False
            else:
                civil_grounded, _ = validate_civil_answer(result_text, tool_result)
                if not civil_grounded:
                    result_text = grounding_reply("ungrounded", ctx.req.lang)
            yield sse("answer_chunk", result_text)
        else:
            result_text = await ensure_answer(ctx, result_text,
                f"简单任务快速通道未返回有效回复: agent={agent_name}, models={ctx.model_try_list}")

        cache_allowed = state["generated"] and not state["interrupted"] and civil_grounded
        if state["sf"].contains_sensitive(result_text):
            cache_allowed = False
        result_text = apply_safety_filter(result_text)
        if agent_name == "search_knowledge":
            _record_ungrounded_citations(result_text, tool_result)
        yield sse("answer_complete", result_text) + "data: [DONE]\n\n"
        ctx.saved_normally = True  # 先置位再 finalize（C-F11，F2 顺序统一）
        ctx.finished = True
        await finalize_answer(ctx, result_text, write_cache=cache_allowed)
        logger.info(f"简单任务快速通道完成: agent={agent_name}")
    except Exception as fast_err:
        if is_civil_persona(ctx.persona_id):
            logger.warning(f"民法典快速通道失败，禁止通用知识降级: {fast_err}")
            async for event in _finish_simple_terminal(
                    ctx, grounding_reply("service_error", ctx.req.lang)):
                yield event
            return
        # 已有正文外发时不得换模型重答（10-07 审查 chat P2-1 同族旁路，与 chat_react.
        # _fallback_and_finalize 同一口径）：重答会把半截与全文拼成矛盾的两段并双扣费
        if state.get("text"):
            logger.warning(
                f"简单任务快速通道中途失败，已外发 {len(state['text'])} 字，收口不重答: {fast_err}")
            async for event in _finalize_partial_answer(ctx, state["text"]):
                yield event
            return
        logger.warning(f"简单任务快速通道失败，降级到流式模式: {fast_err}")
        # 降级：继续走流式 ReAct 循环（不置 finished，主编排接续）


async def _run_recommend_tools(ctx: ChatStreamCtx, tool_results: List[Tuple[str, dict]]) -> AsyncIterator[str]:
    """并行执行推荐通道的旅游四工具；as_completed 逐个完成立即输出事件"""
    async def _run_one(agent: str) -> tuple:
        """执行单个工具；异常归一为 {error} 并带回 agent 名（供结果归位）"""
        try:
            res = await dispatch_tool(
                agent, {}, ctx.user_query, None, getattr(ctx, "username", ""))
        except Exception as te:
            res = {"error": sanitize_error_text(str(te))}
        return agent, res

    touring = [a for a in ctx.matched_agents if a in RECOMMEND_TOUR_TOOLS]
    # spawn 持强引用（2026-09-14 逐行审查补）：裸 create_task 在生成器被早闭
    # （客户端断连触发 GeneratorExit）时局部 tasks 引用随帧销毁，在途任务成孤儿
    from ..core.concurrency import spawn
    tasks = [spawn(_run_one(agent), name=f"recommend:{agent}") for agent in touring]
    try:
        for done in asyncio.as_completed(tasks, timeout=TOOL_TIMEOUT):
            name, res = await done
            tool_results.append((name, res))
            if name == "query_weather":
                await persist_weather_snapshot(getattr(ctx, "mm", None), res)
            yield f"data: {json.dumps({'type': 'tool_call', 'name': name, 'args': {}})}\n\n"
            yield f"data: {json.dumps({'type': 'tool_result', 'name': name, 'index': 0, 'result': display_safe_tool_result(res)})}\n\n"
    except TimeoutError:
        for t in tasks:
            if not t.done():
                t.cancel()
        for name in touring:
            if name not in {n for n, _ in tool_results}:
                tool_results.append((name, {"error": "tool timeout"}))
                yield f"data: {json.dumps({'type': 'tool_call', 'name': name, 'args': {}})}\n\n"
                yield f"data: {json.dumps({'type': 'tool_result', 'name': name, 'index': 0, 'result': {'error': 'tool timeout'}})}\n\n"


async def recommend_fast_path(ctx: ChatStreamCtx) -> AsyncIterator[str]:
    """推荐/多 Agent：已知意图 → 直接并行执行所有工具 → 单次 LLM 格式化"""
    mark_route("recommend_fast_path")
    logger.info(f"推荐/多Agent快速通道: agents={ctx.matched_agents}")
    yield sse("thought", thinking_text("recommend_multi", ctx.req))
    result_text = ""  # 声明在 try 外：except 分支要能读到"已外发了多少"（10-07 chat P2-1 旁路）
    try:
        tool_results: List[Tuple[str, dict]] = []
        async for ev in _run_recommend_tools(ctx, tool_results):
            yield ev

        _sys = _fast_system_base(ctx)
        # 文件上下文不再拼接（F1，2026-09-28，与 simple 通道同修）：ctx.messages[1:] 已承载，重复拼=双倍 token
        # 错误结果不进 prompt：模型利用不了错误数据只会元推理，缺失板块只点名做一句中性说明
        ok_results = [(n, r) for n, r in tool_results if not (isinstance(r, dict) and r.get("error"))]
        _sys += "\n\n以下是已查询到的实时数据，请整合成一份完整、自然、连贯的回答给用户。不要提及「专家」「查询工具」等词。\n\n"
        for name, res in ok_results:
            _sys += f"--- {name} ---\n{format_untrusted_tool_context(res)}\n\n"
        missing = [n for n, r in tool_results if isinstance(r, dict) and r.get("error")]
        if missing:
            _missing_zh = "、".join(_MISSING_SECTION_ZH.get(n, n) for n in missing)
            _sys += (
                f"以下部分本次没有查询到数据：{_missing_zh}。"
                f"相关部分请基于你自己的知识给出建议，不要编造具体实时数字。\n\n"
            )
        _sys += _ANSWER_DISCIPLINE

        _sf = get_filter()
        _content_guard = ContentStreamGuard(_sf)
        _stream_interrupted = False
        _stream_generated = False
        _messages = _messages_for_fast_path(ctx, _sys)
        async for kind, text in answer_via_models(ctx, _messages, "多Agent"):
            if kind == "reasoning":
                # 与 ReAct 路径同口径的思考外发门禁（2026-09-19 审查 chat P2-2）
                if not hide_reasoning(ctx.persona_id, ctx.persona) and text:
                    yield sse("reasoning_chunk", text)
            elif kind == "answer":
                _piece, _blocked = _content_guard.feed(text)  # 跨块缓冲拦被块边界切开的敏感词（09-10 P2）
                if _blocked:
                    result_text += safety_message(ctx.req, _sf.safe_message)
                    yield sse("answer_chunk", safety_message(ctx.req, _sf.safe_message))
                    _stream_interrupted = True
                    break
                if _piece:
                    _stream_generated = True
                    result_text += _piece
                    yield sse("answer_chunk", _piece)
        if not _stream_interrupted:
            _tail, _tail_blocked = _content_guard.flush()
            if _tail_blocked:
                _stream_interrupted = True
                result_text = safety_message(ctx.req, _sf.safe_message)
                yield sse("answer_chunk", safety_message(ctx.req, _sf.safe_message))
            elif _tail:
                _stream_generated = True
                result_text += _tail
                yield sse("answer_chunk", _tail)

        result_text = await ensure_answer(ctx, result_text,
            f"多Agent快速通道未返回有效回复: agents={ctx.matched_agents}, models={ctx.model_try_list}")
        _cache_allowed = _stream_generated and not _stream_interrupted  # DFA 拦过的产物不入语义缓存（09-11 P1）
        if _sf.contains_sensitive(result_text):
            _cache_allowed = False
        result_text = apply_safety_filter(result_text)
        yield sse("answer_complete", result_text) + "data: [DONE]\n\n"

        ctx.saved_normally = True  # 先置位再 finalize（C-F11，F2 顺序统一）
        ctx.finished = True
        await finalize_answer(ctx, result_text, write_cache=_cache_allowed)
        logger.info(f"多Agent快速通道完成: {len(tool_results)}个工具")
        return
    except Exception as fast_err:
        if result_text:  # 同 simple 通道：半截正文已外发则不重答，只补完成标记收口
            logger.warning(
                f"多Agent快速通道中途失败，已外发 {len(result_text)} 字，收口不重答: {fast_err}")
            async for event in _finalize_partial_answer(ctx, result_text):
                yield event
            return
        logger.warning(f"多Agent快速通道失败，降级到流式模式: {fast_err}")

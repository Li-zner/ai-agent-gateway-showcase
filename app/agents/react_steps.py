"""
ReAct 步骤层 — 从 runner.py 拆出的单步执行原语（行为等价移动，2026-09-05）。

runner.run_agent_task 只做编排（缓存/锁/路由/收尾），本模块负责：
  - _consume_llm_stream : 单轮 LLM 流式消费（逐块 DFA 过滤 + 实时写 Redis）
  - _handle_tool_step   : 工具执行 + 多 Agent 讨论 + 工具消息回填
  - _stream_fallback    : 降级链输出
  - _run_discussion     : 圆桌讨论摘要
  - _finish_cancelled   : 取消收尾（runner 与本模块共用）
"""

import asyncio
import json
import uuid
import httpx

from ..core.logging import setup_logging
from ..core.persona_manager import is_civil_persona
from ..core.config import (
    TOOL_TIMEOUT, DEEPSEEK_API_BASE, LLM_PRIMARY_MODEL,
    LLM_FALLBACK_MODEL, HTTP_TIMEOUT_LONG,
)
from ..core.safety_filter import (
    ContentStreamGuard, sanitize_error_text, sanitize_untrusted_text,
    serialize_untrusted_value,
)
from ..core.stream_utils import dispatch_tool, stream_llm
from ..core.task_manager import (
    append_result, read_accumulated_result,
    is_cancelled_remote, cancel_task_if_active, clear_cancel_marker,
    reset_accumulated_result,
)
from ..services.conversation_profiles import persist_weather_snapshot

logger = setup_logging()


async def _finish_cancelled(task_id: str, partial: str):
    """标记任务为已取消，保存草稿（runner 编排层与步骤层共用）"""
    await cancel_task_if_active(task_id, partial)
    await clear_cancel_marker(task_id)
    logger.info(f"任务已取消: task_id={task_id}")


async def _tool_arg_error(func_name: str) -> dict:
    """LLM 工具参数不是合法 JSON 时的占位结果：按工具失败处理，与真实工具错误走同一条兜底链。

    必须是协程：调用方把返回值直接 append 进 gather 的任务列表，
    同步 dict 会当场 TypeError 炸掉整个任务（2026-09-06 修复）。
    """
    return {"error": f"{func_name} 参数不是有效 JSON"}


async def _consume_llm_stream(task_id: str, api_key: str, messages: list, tools: list, sf) -> tuple:
    """消费一次 LLM 流式响应：内容逐块 DFA 过滤并实时写入 Redis（轮询端立即可见）。

    返回 (state, done)：state 含 content/tool_calls 聚合/usage/blocked；
    done=True 表示任务已因取消而终结，调用方直接返回。
    """
    _content_guard = ContentStreamGuard(sf)
    from ..services.chat_support import attach_bill_uid, safety_message
    state = {
        "content": "", "has_tool_calls": False,
        "tool_calls_index": {},
        "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        "blocked": False,
    }
    # 统一走 stream_llm（ReAct 路径保持模型默认 temperature）
    _agen = stream_llm(api_key, LLM_PRIMARY_MODEL, messages, tools=tools,
                       tool_choice="auto", temperature=None, max_tokens=None)
    _stream_blocked = False
    async for _ev in _agen:
        if _ev["type"] == "usage":
            state["usage"]["prompt_tokens"] = _ev["prompt_tokens"]
            state["usage"]["completion_tokens"] = _ev["completion_tokens"]
            # 扣费幂等键（P1-②，2026-10-11）：uid 由 stream_llm 每次上游调用生成并随
            # usage 事件带出，原先这里只拷 token 数把它丢了 → runner 扣费无载体
            attach_bill_uid(state["usage"], _ev.get("bill_uid") or "")
            if await is_cancelled_remote(task_id):
                return state, True
        elif _ev["type"] == "content":
            chunk = _ev["text"]
            piece, blocked = _content_guard.feed(chunk)
            if blocked:
                state["content"] += safety_message(None, sf.safe_message)
                await append_result(task_id, safety_message(None, sf.safe_message))
                state["blocked"] = True
                _stream_blocked = True
                # 拦截后排空内层流拿 usage 尾块计费（原代码 break 遗弃生成器）
                async for _rest in _agen:
                    if _rest["type"] == "usage":
                        state["usage"]["prompt_tokens"] = _rest["prompt_tokens"]
                        state["usage"]["completion_tokens"] = _rest["completion_tokens"]
                        # 同一次上游调用：沿用已有键，不覆盖
                        attach_bill_uid(state["usage"], _rest.get("bill_uid") or "")
                break
            if piece:
                state["content"] += piece
                await append_result(task_id, piece)
        elif _ev["type"] == "tool_calls":
            state["has_tool_calls"] = True
            for tc in _ev["delta"]:
                idx = tc.get("index")
                if idx is None:
                    continue
                if idx not in state["tool_calls_index"]:
                    state["tool_calls_index"][idx] = {"id": tc.get("id", ""), "type": tc.get("type", "function"), "function": {"name": "", "arguments": ""}}
                if tc.get("id"):
                    state["tool_calls_index"][idx]["id"] = tc["id"]
                if tc.get("function"):
                    if tc["function"].get("name"):
                        state["tool_calls_index"][idx]["function"]["name"] = tc["function"]["name"]
                    if tc["function"].get("arguments"):
                        state["tool_calls_index"][idx]["function"]["arguments"] += tc["function"]["arguments"]
    if not _stream_blocked:
        tail, tail_blocked = _content_guard.flush()
        if tail_blocked:
            state["content"] += safety_message(None, sf.safe_message)
            await append_result(task_id, safety_message(None, sf.safe_message))
            state["blocked"] = True
        elif tail:
            state["content"] += tail
            await append_result(task_id, tail)
    return state, False


async def _run_discussion(user_query: str,
                          tool_calls_list: list, tool_results: list, intent,
                          username: str = "") -> str:
    """多 Agent 圆桌讨论（只用匹配到的 Agent），返回讨论摘要；异常返回空串不中断任务。

    username 透传给编排器供圆桌 LLM 计费（2026-09-09 主人拍板）。
    """
    try:
        from .orchestrator import AgentOrchestrator
        from .router import get_agent_names_for_orchestrator
        orch = AgentOrchestrator(user_query, username=username)
        matched_agents = intent.get("agents", []) if intent else []
        agent_names = get_agent_names_for_orchestrator(matched_agents)
        if not agent_names:
            agent_names = ["query_weather", "query_hotel", "query_route", "query_food"]
        for idx, tc in enumerate(tool_calls_list):
            func_name = tc["function"]["name"]
            result = tool_results[idx] if idx < len(tool_results) else {"error": "missing"}
            if isinstance(result, Exception):
                result = {"error": str(result)}
            if func_name in agent_names:
                if isinstance(result, dict) and "error" in result:
                    orch.add_tool_result(
                        func_name, {}, error=sanitize_untrusted_text(str(result["error"])))
                else:
                    orch.add_tool_result(
                        func_name,
                        {"untrusted_tool_data": serialize_untrusted_value(result)},
                    )
        if orch.get_involved_agents() and len(agent_names) > 1:
            from ..core.config import ORCH_ENABLE_PHASE2
            return await orch.run(enable_phase2=ORCH_ENABLE_PHASE2)
        return ""
    except Exception as orch_err:
        logger.warning(f"多Agent讨论异常: {orch_err}")
        return ""


async def _handle_tool_step(task_id: str, username: str, user_query: str,
                            assistant_content: str, messages: list, tool_calls_list: list,
                            intent, user_perms, persona_id: str = "",
                            mm=None) -> str:
    """执行本轮工具调用并按结果续写对话（工具消息 + 多 Agent 讨论）。

    返回 "ok"（继续下一轮）/ "cancelled"（降级中任务已取消）/ "timeout"（工具超时已降级，
    调用方应结束循环走 max_steps 收尾）。
    """
    tasks = []
    for tc in tool_calls_list:
        func_name = tc["function"]["name"]
        try:
            args = json.loads(tc["function"]["arguments"])
        except (json.JSONDecodeError, TypeError):
            # LLM 偶发产出非法 JSON 参数：按工具失败处理，而不是炸掉整个任务
            tasks.append(_tool_arg_error(func_name))
        else:
            if persona_id:
                tasks.append(dispatch_tool(
                    func_name, args, user_query, user_perms, username,
                    persona_id=persona_id,
                ))
            else:
                tasks.append(dispatch_tool(
                    func_name, args, user_query, user_perms, username,
                ))

    try:
        tool_results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout=TOOL_TIMEOUT
        )
    except asyncio.TimeoutError:
        logger.warning("工具超时，降级")
        if await _stream_fallback(task_id, user_query, username, persona_id=persona_id):
            return "cancelled"
        return "timeout"

    discussion_summary = await _run_discussion(
        user_query, tool_calls_list, tool_results, intent, username=username)

    # 工具结果喂回模型：错误工具换成友好兜底文案，不把原始报错透给用户
    tool_msgs = []
    for idx, result in enumerate(tool_results):
        func_name = tool_calls_list[idx]["function"]["name"]
        if isinstance(result, Exception):
            # CHAT-4（2026-09-20 审查）：异常 str 可含带 key 的完整 URL，剥指纹后
            # 再进 LLM 上下文（与 chat_react 同口径；"未找到/超时"关键字不受影响）
            result = {"error": sanitize_error_text(str(result))}
        if func_name == "query_weather":
            await persist_weather_snapshot(mm, result)
        if isinstance(result, dict) and "error" in result:
            err = result["error"]
            fb = "获取数据失败了，可能服务暂时不可用。"
            if "未找到" in err:
                fb = "暂时没找到这个城市的数据"
            elif "超时" in err:
                fb = "查询超时，请稍后再试"
            result = {"error": err, "fallback_message": fb, "success": False}
        # tool 角色本身是不可信边界，内容再递归消毒后序列化，避免 title/body
        # 等外部字段携带提示词注入或控制字符。
        tool_msgs.append({
            "role": "tool",
            "tool_call_id": tool_calls_list[idx]["id"],
            "content": serialize_untrusted_value(result),
        })

    messages.append({
        "role": "assistant",
        "content": assistant_content,
        "tool_calls": tool_calls_list
    })
    messages.extend(tool_msgs)
    if discussion_summary:
        messages.append({
            "role": "system",
            "content": f"以下是各领域专家对结果的讨论分析，请参考这些专业意见来回答用户：\n\n{discussion_summary}"
        })
    return "ok"


async def _stream_fallback(task_id: str, user_query: str, username: str,
                           persona_id: str = "") -> bool:
    """降级链输出到 Redis（主模型失败/工具超时时）。返回 True 表示降级中任务被取消（已终结）。"""
    if is_civil_persona(persona_id):
        from ..services.civil_grounding import grounding_reply
        # grounding 话术是"整段替换"语义而非补充：主模型中途失败时 result_buf 里
        # 已有半截回答，直接 append 会拼出两段互相矛盾的正文（2026-09-19 审查 agents P2-1）
        await reset_accumulated_result(task_id)
        await append_result(task_id, grounding_reply("service_error"))
        return False
    # 主模型可能已写出安全前缀；降级回答必须替换而不是追加，否则最终结果会
    # 变成“半截主回答 + 完整降级回答”两段互相矛盾的内容。
    await reset_accumulated_result(task_id)
    async for chunk in _fallback_chain(user_query, username):
        if await is_cancelled_remote(task_id):
            await _finish_cancelled(task_id, await read_accumulated_result(task_id))
            return True
        await append_result(task_id, chunk)
    return False


async def _fallback_chain(user_query: str, username: str):
    """降级链：仅 DeepSeek Flash；Flash 也失败则给友好兜底（P1 #37）"""
    yielded = False
    try:
        async for chunk in _fallback_flash(user_query, username):
            yielded = True
            yield chunk
    except Exception as e:
        # Flash 侧已带日志；链上剩余异常吞掉，交给下方 not-yielded 兜底文案（P2 修复：不再裸 pass）
        logger.debug(f"降级链异常: {e}")
    if not yielded:
        # 答案级本地化：Flash 也失败时的兜底文案按界面语言切换（contextvar 由 runner 入口登记）
        from ..services.chat_support import fallback_message
        yield fallback_message()


async def _fallback_flash(user_query: str, username: str):
    """降级：DeepSeek Flash"""
    from ..services.chat_support import attach_bill_uid, get_deepseek_key, safety_message
    from ..services.llm_streaming import _estimate_tokens, record_token_usage
    # 本次上游调用一个扣费幂等键：真实 usage 尾块与断连估算同属这一笔费用
    _bill_uid = uuid.uuid4().hex
    api_key = await get_deepseek_key()
    if not api_key:
        return
    from ..core.safety_filter import get_filter
    sf = get_filter()
    guard = ContentStreamGuard(sf)
    _usage = None  # include_usage 尾块用量（2026-09-09 计费收口）
    _full = ""     # 断连估算用已产出累积（2026-09-09 P1）
    _blocked = False
    # CHAT-5（2026-09-20 审查）：任务侧降级此前不持 llm_semaphore（SSE 侧
    # chat_fallback._flash_sse 持），故障风暴时任务路径绕过全局 LLM 并发闸；
    # 与 chat_fallback 同法——整段流式消费持锁
    from ..core.concurrency import llm_semaphore
    async with llm_semaphore:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_LONG) as client:
            async with client.stream(
                "POST", f"{DEEPSEEK_API_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": LLM_FALLBACK_MODEL, "messages": [{"role": "user", "content": user_query}],
                      "stream_options": {"include_usage": True}, "max_tokens": 1024},
            ) as resp:
                resp.raise_for_status()
                try:
                    async for line in resp.aiter_lines():
                        if line.startswith("data:"):
                            data_str = line[5:].strip()
                            if data_str == "[DONE]":
                                break
                            try:
                                data = json.loads(data_str)
                                # include_usage 尾块 choices 为空，取值须容忍（同 chat_fallback 修复）
                                if data.get("usage"):
                                    _usage = attach_bill_uid(data["usage"], _bill_uid)
                                choices = data.get("choices") or [{}]
                                chunk = choices[0].get("delta", {}).get("content", "")
                                if chunk:
                                    if _blocked:
                                        continue
                                    _full += chunk
                                    piece, is_blocked = guard.feed(chunk)
                                    if is_blocked:
                                        _blocked = True
                                        yield safety_message(None, sf.safe_message)
                                        continue
                                    if piece:
                                        yield piece
                            except json.JSONDecodeError:
                                continue  # 非数据行/残包：跳过继续读流（P2 修复：不再裸 pass）
                except (GeneratorExit, asyncio.CancelledError):
                    # 断连/取消估算计费（2026-09-09 审查 P1）：直连 httpx 无法安全排空，
                    # 退化按已产出文本估算（正常完成路径已在下方按真实 usage 计费）
                    if username and _full:
                        record_token_usage(
                            attach_bill_uid(
                                {"prompt_tokens": 0, "completion_tokens": _estimate_tokens(_full)},
                                _bill_uid),
                            username, "",
                            remark=f"断连估算补计费（任务降级 {_estimate_tokens(_full)} tokens）")
                    raise
                if not _blocked:
                    tail, tail_blocked = guard.flush()
                    if tail_blocked:
                        yield safety_message(None, sf.safe_message)
                    elif tail:
                        yield tail
                # 计入计费（2026-09-09 主人拍板）：任务降级路径原先零计量
                if _usage:
                    record_token_usage(
                        _usage, username, "", model=LLM_FALLBACK_MODEL,
                    )

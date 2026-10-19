"""
Agent 后台任务运行器 — 编排层（2026-09-05 重构：步骤原语拆至 react_steps.py，行为等价）。

职责：从任务管理器获取控制信号，串起 语义缓存/重建锁 → 意图路由 → ReAct 主循环，
将结果逐步写入 Redis（而非流式推送）。被 v2 路由中的 asyncio.create_task 启动，
独立于 HTTP 请求生命周期。
"""

import asyncio
import uuid

from ..core.logging import setup_logging
from ..core.persona_manager import is_civil_persona
from ..core.config import SUMMARY_THRESHOLD, LLM_PRIMARY_MODEL
from ..core.memory_manager import MemoryManager
from ..core.task_manager import (
    update_status,  # noqa: F401 — 本模块命名空间绑定，task_stages 经 getattr 晚绑定回读
    append_result, read_accumulated_result,
    is_cancelled_remote, finish_task, fail_task,
    cleanup_event, clear_cancel_marker, get_task,
)
from ..core.semantic_cache import SemanticCache, safe_set
from ..core.concurrency import spawn
from .router import classify_intent, handle_simple_task
from .react_steps import (
    _consume_llm_stream, _handle_tool_step, _stream_fallback, _finish_cancelled,
)
from .memory import compress_message_history
from .orchestrator import build_shared_context
from . import task_stages

logger = setup_logging()

async def _try_cache_hit(task_id: str, username: str, user_query: str, mm, cache_ctx: str = "") -> bool:
    """语义缓存拦截：命中则分块写入 Redis 并返回 True（调用方直接返回）"""
    try:
        cached = await SemanticCache.get(user_query, cache_ctx=cache_ctx)
        # 穿透占位（__EMPTY__）不是答案：SSE 路径写入的短 TTL 标记，
        # 任务路径必须放行走正常生成，否则字面 "__EMPTY__" 会发给用户
        if not cached or await SemanticCache.is_empty(cached):
            return False
        from ..core.safety_filter import get_filter
        sf = get_filter()
        if sf.contains_sensitive(cached):
            from ..services.chat_support import safety_message
            cached = safety_message(None, sf.safe_message)
        # 分块写入 Redis
        for i in range(0, len(cached), 80):
            chunk = cached[i:i + 80]
            await append_result(task_id, chunk)
            if await is_cancelled_remote(task_id):
                await _finish_cancelled(task_id, await read_accumulated_result(task_id))
                return True
        await finish_task(task_id, cached)
        # 用户消息由 save_messages 统一保存（含去重），不再先 save_user_message 一遍（P2 修复冗余）
        await mm.save_messages(
            {"role": "user", "content": user_query},
            {"role": "assistant", "content": cached}
        )
        return True
    except Exception:
        # 缓存查询失败不影响正常推理（放行走正常生成）
        return False


async def _wait_cache_or_busy(task_id: str, username: str, user_query: str, mm,
                              cache_ctx: str) -> tuple:
    """语义缓存拦截 + 防击穿重建锁（P0 #1/#32）。

    未命中时获取重建锁，仅一个请求重建；拿不到锁则等待重读缓存（最多 15 秒），
    仍拿不到说明别家持有锁且重建慢——不能在未持锁状态下重复调用 LLM 并写缓存
    （多个超时任务同时重建会击穿防击穿机制，缓存雪崩），返回降级完成（P1）。

    返回 (rebuild_lock_token, served)：served=True 表示任务已终结，调用方直接返回；
    token 需由调用方在 finally 释放（仅持有者释放，失败由 TTL 自愈）。
    """
    if await _try_cache_hit(task_id, username, user_query, mm, cache_ctx=cache_ctx):
        return None, True

    token = await SemanticCache.acquire_rebuild_lock(user_query, cache_ctx=cache_ctx, ttl=45)
    if token is not None:
        return token, False

    # 已有请求在重建：等待后重读缓存（最多 15 秒）
    for _ in range(30):
        await asyncio.sleep(0.5)
        if await is_cancelled_remote(task_id):
            await _finish_cancelled(task_id, await read_accumulated_result(task_id))
            return None, True
        if await _try_cache_hit(task_id, username, user_query, mm, cache_ctx=cache_ctx):
            return None, True
    # 15 秒仍拿不到锁：降级完成而非并发重建（P1）
    token = await SemanticCache.acquire_rebuild_lock(user_query, cache_ctx=cache_ctx, ttl=45)
    if token is None:
        await finish_task(task_id, "服务繁忙，请稍后再试。")
        return None, True
    return token, False


async def _renew_until_done(user_query: str, token: str, cache_ctx: str):
    """周期续期重建锁（仅持有者可续，P0 #50 落地）：长任务重建期间锁不过期，防并发重复重建。

    对端返回 False（已不持有，如 TTL 曾过期被夺）即停止；由调用方 finally 取消本协程。
    """
    while True:
        await asyncio.sleep(15)  # TTL 45s，15s 续一次留足裕量
        try:
            if not await SemanticCache.renew_rebuild_lock(user_query, token, cache_ctx=cache_ctx):
                return
        except Exception as e:
            # 单次续期失败（网络抖动）不放弃持有权，下轮重试；最终由持有者释放/TTL 兜底
            logger.warning(f"重建锁续期失败（下轮重试）: {e}")


async def _maybe_simple_task(task_id: str, username: str, conversation_id: str,
                             user_query: str, persona_id: str,
                             file_ids, cache_ctx: str | None, lang: str, user_perms) -> tuple:
    """意图路由（步骤 1.5）：简单任务直接执行单个 Agent 并终结任务。

    返回 (handled, intent)：handled=True 调用方直接返回；复杂任务/纯 LLM 返回
    (False, intent) 继续主流程；路由异常不中断——非 civil 降为无工具纯 LLM
    （intent=None），civil 人格返回强制 search_knowledge 意图（与 SSE 链同口径）。
    """
    try:
        if is_civil_persona(persona_id):
            intent = {
                "agents": ["search_knowledge"],
                "is_simple": True,
                "is_recommend": False,
                "method": "civil_forced",
            }
        else:
            from ..core.config import INTENT_USE_LLM
            intent = await classify_intent(user_query, use_llm=INTENT_USE_LLM, username=username)
        logger.info(f"意图分类: agents={intent['agents']}, simple={intent['is_simple']}, method={intent.get('method','?')}")

        if intent["is_simple"] and intent["agents"]:
            # 简单任务 → 直接调用单个 Agent，不走四 Agent 圆桌
            agent_name = intent["agents"][0]
            logger.info(f"路由为简单任务: agent={agent_name}, task_id={task_id}")
            await handle_simple_task(
                task_id=task_id, username=username, conversation_id=conversation_id,
                user_query=user_query, agent_name=agent_name,
                persona_id=persona_id, file_ids=file_ids, cache_ctx=cache_ctx,
                lang=lang, user_perms=user_perms,
            )
            return True, intent
        elif not intent["agents"]:
            # 未匹配到任何工具 → 走纯 LLM 回答（不加工具调用），继续主流程
            logger.info(f"未匹配工具领域，走纯 LLM 回答: task_id={task_id}")
        else:
            logger.info(f"路由为复杂任务: agents={intent['agents']}, task_id={task_id}")
    except Exception as route_err:
        if is_civil_persona(persona_id):
            # 2026-09-25 修两链不对称：civil 人格异常（如 handle_simple_task 抛错）
            # 不得掉进无工具纯 LLM——与 SSE 链 route_intent 异常分支同口径锁知识库
            logger.warning(f"意图路由异常（civil 人格降级为强制 search_knowledge）: {route_err}")
            return False, {"agents": ["search_knowledge"], "is_simple": True,
                           "is_recommend": False, "method": "route_error_fallback"}
        logger.warning(f"意图路由异常（降级为无工具纯 LLM，继续主流程）: {route_err}")
        return False, None
    return False, intent


async def _resolve_handled_outcome(task_id: str, username: str) -> str:
    """简单任务已自行终结，按 Redis 真实终态回传 outcome 并处理未产出额度。"""
    task = await get_task(task_id)
    status = task.get("status") if task else "error"
    if status == "completed":
        return "success"
    if status in ("error", "timeout"):
        await _refund_quota_if_unproduced(username, task_id)
        return "failed"
    if status == "cancelled":
        await _refund_quota_if_unproduced(username, task_id)
        return "cancelled"
    logger.warning(f"简单任务未写入终态，按失败收口: task_id={task_id}, status={status}")
    await _refund_quota_if_unproduced(username, task_id)
    return "failed"


async def _finish_text_answer(task_id: str, user_query: str, mm, full_content: str,
                              cache_ctx: str | None, sf, persona_id: str = "",
                              write_cache: bool = True) -> None:
    """纯文本回答收尾：跨块敏感词终检 → 保存记忆 → 写语义缓存 → 标记完成。"""
    # DFA 终检兜底：check_stream 逐块检查拦不住跨块拼接的敏感词，只有这里能拦
    # （ponytail: 跨块词此时已进轮询缓冲只能补记不能撤回；彻底解决需在 append 侧做跨块
    #  滑动窗口，敏感词表目前是整词命中，跨块概率极低，暂不值得）
    from ..services.chat_support import empty_answer
    final_answer = full_content or empty_answer()
    cache_allowed = write_cache and bool(full_content)
    if sf.contains_sensitive(final_answer):
        from ..services.chat_support import safety_message, content_filtered_note
        final_answer = safety_message(None, sf.safe_message)
        cache_allowed = False
        await append_result(task_id, "\n\n" + content_filtered_note())

    # 先以 CAS 确认任务仍是 active。取消/超时后不得再写长期记忆或缓存。
    completed = await finish_task(task_id, final_answer)
    if not completed:
        logger.info(f"任务已进入终态，跳过文本收尾副作用: task_id={task_id}")
        return

    try:
        from ..services.answer_trace import record_answer
        record_answer(
            user_query, final_answer, getattr(mm, "user_id", ""),
            persona=persona_id or "civil_code",
        )
    except Exception as _trace_err:
        logger.debug(f"答案 trace 创建失败（不影响主流程）: {_trace_err}")

    # 保存到记忆
    try:
        await mm.save_messages(
            {"role": "user", "content": user_query},
            {"role": "assistant", "content": final_answer}
        )
    except Exception as mem_err:
        logger.warning(f"任务记忆保存失败（结果已交付）: {mem_err}")
    if cache_ctx is not None and cache_allowed:
        spawn(safe_set(user_query, final_answer, cache_ctx=cache_ctx),
              name=f"semantic-cache:{task_id}")

    # 请求级 trace 的 answer 阶段（2026-09-14 审计 P1：任务路径此前无任何 span）
    try:
        from ..services.rag_request_trace import record_span
        record_span("answer", result_count=1,
                    attributes={"answer_len": len(final_answer)})
    except Exception as _span_err:  # 监测采集绝不影响回答主流程
        logger.debug(f"answer span 记录失败（不影响主流程）: {_span_err}")

    logger.info(f"任务完成: task_id={task_id}")


async def _finish_task_max_steps(task_id: str, user_query: str, mm, cache_ctx: str | None,
                                 persona_id: str) -> None:
    """ReAct 达到最大轮次后的统一收尾；民法典禁止落到通用知识文案。"""
    from ..core.safety_filter import get_filter
    final_text = await read_accumulated_result(task_id)
    if not final_text:
        if is_civil_persona(persona_id):
            from ..services.civil_grounding import grounding_reply
            final_text = grounding_reply("service_error")
        else:
            from ..services.chat_support import empty_answer
            final_text = empty_answer()
    await _finish_text_answer(
        task_id, user_query, mm, final_text, cache_ctx, get_filter(),
        persona_id=persona_id, write_cache=False,
    )


async def _run_react_loop(task_id: str, username: str, user_query: str,
                          mm, messages: list, tools: list, intent, cache_ctx: str | None,
                          user_perms, api_key: str, persona_id: str = "") -> None:
    """ReAct 主循环（步骤 3，最多 max_steps 轮）：LLM 流式 → 工具 → 再 LLM，直到纯文本回答。

    步骤原语（流式消费/工具执行/降级）在 react_steps.py；本函数只做轮次编排与计量。
    """
    from ..core.metrics import llm_tokens_total, llm_tokens_detail, llm_requests_total
    from ..core.safety_filter import get_filter
    max_steps = 3
    for step in range(max_steps):
        if await is_cancelled_remote(task_id):
            await _finish_cancelled(task_id, await read_accumulated_result(task_id))
            return

        # 上下文截断：防 messages 无限累积撑爆上下文窗口（P0 #60）
        messages = compress_message_history(messages, max_messages=10)
        sf = get_filter()

        try:
            state, done = await _consume_llm_stream(task_id, api_key, messages, tools, sf)
        except Exception as e:
            from ..core.metrics import llm_requests_total as _lrt
            _lrt.labels(model=LLM_PRIMARY_MODEL, endpoint='v2_task', status='error').inc()  # 2026-09-11 审查 P1：告警接线
            logger.warning(f"DeepSeek API 失败 (step {step}): {e}")
            # 降级走 fallback（Flash）；降级中被取消则任务已终结
            if await _stream_fallback(
                    task_id, user_query, username, persona_id=persona_id):
                return
            llm_requests_total.labels(model=LLM_PRIMARY_MODEL, endpoint='v2_task', status='fallback').inc()
            break

        # ---- Token 计量 ----
        pt = state["usage"]["prompt_tokens"]
        ct = state["usage"]["completion_tokens"]
        if pt or ct:
            llm_tokens_total.labels(type='input').inc(pt)
            llm_tokens_total.labels(type='output').inc(ct)
            llm_tokens_detail.labels(model=LLM_PRIMARY_MODEL, endpoint='v2_task', type='input').inc(pt)
            llm_tokens_detail.labels(model=LLM_PRIMARY_MODEL, endpoint='v2_task', type='output').inc(ct)
        llm_requests_total.labels(model=LLM_PRIMARY_MODEL, endpoint='v2_task', status='success').inc()
        # 2026-09-09 审查 P0：任务路径原先只打指标，从不扣费也不计日 token
        # （"旁路漏扣费"历史教训同类）；指标已由上方 v2_task 打点，复用不含指标的
        # bill_token_usage 补齐钱包扣费与日 token 计数，避免双计
        from ..services.llm_streaming import bill_token_usage
        bill_token_usage(state["usage"], username, task_id,
                         remark=f"Agent任务消耗 {pt + ct} tokens（输入 {pt} + 输出 {ct}）")
        if done:
            await _finish_cancelled(task_id, await read_accumulated_result(task_id))
            return

        # 检查是否有工具调用（内容被 DFA 拦截时不进入工具分支，直接走完成路径）
        if state["has_tool_calls"] and state["tool_calls_index"] and not state["blocked"]:
            tool_calls_list = [
                {"id": v["id"], "type": v["type"],
                 "function": {"name": v["function"]["name"], "arguments": v["function"]["arguments"]}}
                for v in state["tool_calls_index"].values()
            ]
            branch = await _handle_tool_step(
                task_id, username, user_query, state["content"],
                messages, tool_calls_list, intent, user_perms,
                persona_id=persona_id, mm=mm,
            )
            if branch == "cancelled":
                return
            if branch == "timeout":
                break
        else:
            # 纯文本回答完成。CHAT-2（09-20 审查）：DFA 拦截产物不得写缓存——
            # "安全前缀+安全文案"会绕过终检 == safe_message 精确比对
            await _finish_text_answer(
                task_id, user_query, mm, state["content"], cache_ctx, sf,
                persona_id=persona_id, write_cache=not state["blocked"],
            )
            return

    # max_steps 耗尽（含主模型失败走降级链、工具超时两种 break 收尾）：
    # 与正常纯文本回答同一收口。2026-09-14 审计 P1：原先只 finish_task——
    # 降级回答不进记忆（追问失忆）、不写语义缓存（无法复用）
    await _finish_task_max_steps(task_id, user_query, mm, cache_ctx, persona_id)


async def _build_cache_ctx(mm, username: str, persona_id: str,
                           user_query: str, lang: str = "",
                           file_ids=None, user_perms=None) -> tuple[str, bool]:
    """返回缓存上下文和是否可缓存；已有历史**或带文件**的请求跳过缓存。

    模型维度在任务路径此处尚未解析，留空即可：缓存键不同只会少命中，不会串答案。
    2026-09-28 L1：带 file_ids 不吃缓存——对齐流式路 serve_from_cache 的豁免条件
    （chat_stream_core），否则同用户换文件问同一句会回放旧文件的答案（缓存键
    不含文件维度）。L5：cache_ctx 并入权限指纹，升级 vip 后不再命中旧公开权限回答。
    """
    try:
        _task_profile = await mm.get_profile()
    except Exception as _tp_err:
        logger.warning(f"任务画像加载失败（按无画像处理）: {_tp_err}")
        _task_profile = ""
    # 独立首轮问答允许跨会话复用；有历史时 cacheable=False，避免上下文串答。
    cache_ctx = SemanticCache.build_cache_ctx(
        username, persona_id, _task_profile, "", user_query=user_query,
        lang=lang or "", user_perms=user_perms,
    )
    try:
        history = await mm.get_context(limit=1) if mm is not None else []
    except Exception as history_err:
        logger.warning(f"任务历史探测失败，跳过缓存: {history_err}")
        history = [object()]
    cacheable = not bool(history) and not file_ids
    return cache_ctx, cacheable


async def _enter_cache_gate(task_id: str, username: str, user_query: str,
                            mm, cache_ctx: str) -> tuple:
    """步骤 1：语义缓存拦截 + 防击穿重建锁。返回 (token, renew_task, served)。"""
    token, served = await _wait_cache_or_busy(task_id, username, user_query, mm, cache_ctx)
    if served:
        return None, None, True
    renew_task = spawn(
        _renew_until_done(user_query, token, cache_ctx),
        name=f"renew-lock:{task_id}")
    return token, renew_task, False


async def _prepare_task_run(task_id: str, username: str, mm, user_query: str,
                            persona_id: str, file_ids, intent, lang: str) -> tuple | None:
    """步骤 2：取密钥 → 构建消息 → 落用户消息。密钥缺失返回 None（已 fail_task）。

    密钥缺失属内部失败且未产出：此处一并归还试用额度（主人决策 #4A）。
    """
    from ..services.chat_support import get_deepseek_key
    deepseek_api_key = await get_deepseek_key()
    if not deepseek_api_key:
        # 环境变量名不进任务状态（前端会原样展示）
        logger.error("DEEPSEEK_API_KEY 未设置")
        await fail_task(task_id, "服务配置不完整，请联系管理员")
        await _refund_quota_if_unproduced(username, task_id)
        return None
    messages, final_query, tools = await _build_task_messages(
        mm, username, user_query, persona_id, file_ids, intent, lang)
    # 立即保存用户消息
    await mm.save_user_message({"role": "user", "content": user_query})
    return deepseek_api_key, messages, tools


async def run_agent_task(
    task_id: str,
    username: str,
    conversation_id: str,
    user_query: str,
    persona_id: str = "",
    file_ids=None,
    lang: str = "zh",
    # 知识库检索权限：默认仅公开（fail-closed）；None=不过滤只能由 admin 显式传入（P0 修复）。
    # 默认用不可变 () 而非 None/[]：[] 可变默认参是隐患，None 在 search_knowledge
    # 语义是"不过滤"（fail-open），省略调用会变成宽权限（2026-09-07 审查 P2，按语义修正）
    user_perms: list | tuple | None = (),
):
    """后台运行 Agent，逐步写入 Redis；前端轮询 /v2/chat/tasks/{task_id}/result 获取进度。
    编排：语义缓存/重建锁 → 意图路由 → 构建消息 → ReAct 主循环；步骤原语见 react_steps.py。
    生成主体拆至 _task_prepare_generation/_task_gate_and_route（≤80 行门禁），
    本函数只管 trace 生命周期与终态收口。"""
    # uuid 后缀（与 chat_stream_ctx 同一修复）：秒级时间戳同秒并发任务共用会话
    conv_id = conversation_id or f"conv_{username}_{uuid.uuid4().hex[:12]}"
    mm = MemoryManager(username, conv_id, persona_id)
    params = {"persona_id": persona_id, "file_ids": file_ids,
              "lang": lang, "user_perms": user_perms}
    # 登记界面语言：步骤原语（react_steps）无 lang 形参，兜底/安全文案据此切中/英。
    from ..services.chat_support import set_request_lang
    set_request_lang(lang)
    # 后台任务自行建立请求级 trace，finally 统一收尾。
    from ..services.rag_request_trace import begin_request, finish_request, mark_route
    begin_request(conversation_id=conv_id, username=username)
    mark_route("task")
    _outcome = "failed"
    # (cache_ctx, cacheable, rebuild_lock_token, renew_task) 的可变列表：
    # 必须是列表且由本函数创建后传下去——task_stages.gate_and_route 持锁成功后
    # 原位登记，异常路径没有任何返回值，本 finally 只有靠同一只容器才拿得到
    # token（元组/内建列表只能靠返回赋值，抛异常即丢失，09-22 P1）。
    cache_state = ["", False, None, None]
    deadline_task = None
    try:
        # 编排主体必须在 try 内（含缓存上下文构建）：否则异常绕过 fail_task/
        # finally，任务永久停在 pending，取消标记和重建锁也无法清理。
        # 阶段函数定义见 task_stages.py（≤80/≤600 行门禁拆段，行为等价）。
        _outcome_v, deadline_task = await task_stages.prepare_generation(
            task_id, username, user_query, mm, conversation_id, params,
            mark_route, cache_state)
        if _outcome_v is not None:
            _outcome = _outcome_v
            return
    except asyncio.CancelledError:
        _outcome = "cancelled"
        await _finish_cancelled(task_id, await read_accumulated_result(task_id))
    except Exception as e:
        await _fail_task_sanitized(task_id, username, e)
    finally:
        # CORE-1：撤超时闹钟；CAS 保证即使闹钟先触发也不会写坏终态。
        if deadline_task:
            deadline_task.cancel()
        await _release_task_resources(task_id, cache_state[3], cache_state[2],
                                      user_query, cache_state[0])
        try:
            finish_request(status=_outcome)
        except Exception as _fin_err:  # 监测收尾不影响任务主流程
            logger.debug(f"请求级 trace 收尾失败（不影响主流程）: {_fin_err}")


async def _fail_task_sanitized(task_id: str, username: str, e: Exception) -> None:
    """任务异常收尾：剥指纹落 error 终态 + 未产出退额度（2026-09-14 决策 #4A）。"""
    logger.error(f"Agent 任务异常: {e}", exc_info=True)
    from ..core.safety_filter import sanitize_error_text
    # 异常 str 可能带完整 URL/Key，剥指纹后再进任务状态（前端原样展示）
    await fail_task(task_id, sanitize_error_text(str(e)))
    # 内部失败且未产出任何回答：归还试用额度（未产生模型调用/输出时退还，
    # 部分产出不退；rollback 自带 quota_limited 条件，非受限用户是 no-op）
    await _refund_quota_if_unproduced(username, task_id)


async def _refund_quota_if_unproduced(username: str, task_id: str) -> None:
    """任务内部失败且结果缓冲为空（未产出任何回答）时归还一次试用额度。

    主人决策 #4A（2026-09-14 全量审计决策点 4）：未产生模型调用/输出时退还，
    部分输出不退。以 result_buf 是否有内容为"已产出"判据；rollback 的
    quota_limited 条件让非受限用户调用成为 no-op。
    """
    try:
        produced = await read_accumulated_result(task_id)
    except Exception as _read_err:
        logger.debug(f"任务产出读取失败（按未产出处理）: {_read_err}")
        produced = ""
    if produced:
        return
    try:
        from ..core.quota import rollback_used_questions
        await rollback_used_questions(username)
        logger.info(f"任务内部失败未产出，已归还试用额度: task_id={task_id}")
    except Exception as _rb_err:
        logger.warning(f"试用额度归还失败（下次预留以 DB 当前值为准）: {_rb_err}")


async def _release_task_resources(task_id: str, renew_task, rebuild_lock_token,
                                  user_query: str, cache_ctx: str) -> None:
    """任务收尾统一清理：取消事件 + 续期协程 + 重建锁。

    取消事件统一在此清理（2026-09-07 审查 P2，对应 core/task_manager _cancel_events
    慢性泄漏项）：此前散落在各路径，异常/早退路径漏清会慢性泄漏（task_id 为 uuid
    不复用）；pop 幂等，与散落调用并存无害。
    """
    cleanup_event(task_id)
    await clear_cancel_marker(task_id)
    # 取消续期协程；释放重建锁（仅持有者释放；失败由 TTL 自愈，P0 #1）
    if renew_task:
        renew_task.cancel()
    if rebuild_lock_token:
        try:
            await SemanticCache.release_rebuild_lock(user_query, rebuild_lock_token, cache_ctx=cache_ctx)
        except Exception as _rel_err:
            logger.warning(f"重建锁释放失败（由 TTL 自愈，不影响任务结果）: {_rel_err}")


async def _build_task_messages(mm, username: str, user_query: str,
                               persona_id: str, file_ids, intent, lang: str = "zh") -> tuple:
    """准备任务消息：人格 → 历史/画像 → 文件注入 → 工具加载 + 多语言指令。

    返回 (messages, final_query, tools)。
    """
    from ..core.persona_manager import get_persona_manager
    pm = get_persona_manager()
    # 按请求解析人格，不改全局 current（并发任务各用各的，互不覆盖）
    persona = pm.get_effective(persona_id)

    from ..core.constants import today_cn
    today_str = today_cn()
    # safe_format_prompt：人格配置缺占位符时不抛 KeyError（与 SSE 路径 chat_stream_ctx
    # 同一标准；2026-09-14 审计 P2：任务路径原先直接 .format()）
    from ..services.chat_support import safe_format_prompt
    system_content = (
        safe_format_prompt(persona.system_prompt, today=today_str, name=persona.name)
        if persona else f"你是AI助手。今天是{today_str}。"
    )
    # 多语言指令：与流式路径 chat_stream_ctx 一致（P2：任务路径此前漏掉 lang）
    from ..services.chat_support import lang_instruction
    system_content += lang_instruction(lang)

    history_dicts = await mm.get_context(limit=SUMMARY_THRESHOLD)
    user_profile = await mm.get_profile()
    system_content += f"\n{build_shared_context(user_profile or '')}"
    # 2026-09-22 审阅 P2-1（主人拍板"阈值改 20 生效"）：原先 max_messages 与
    # SUMMARY_THRESHOLD 同为 30，而 get_context 已限 30 条，压缩恒 no-op，
    # 30 条全文无界进 prompt；留 10 条压缩余量让旧轮真正折叠。
    history_dicts = compress_message_history(history_dicts, max_messages=20)

    messages = [{"role": "system", "content": system_content}]

    # 上传文件注入：统一走 build_file_context（含提示词注入消毒 + uploaded_by 属主校验，
    # 与流式路径同一标准；原内联实现绕过这两道防线，P1 修复）
    if file_ids:
        from types import SimpleNamespace
        from ..core.stream_utils import build_file_context
        file_ctx = await build_file_context(SimpleNamespace(file_ids=file_ids), username)
        if file_ctx:
            messages.append({"role": "system", "content": file_ctx})

    for msg in history_dicts:
        messages.append(msg)

    # 出发地功能已移除：不再往原话附"（我在X）"，final_query 恒为原话
    final_query = user_query
    messages.append({"role": "user", "content": final_query})
    # 根据意图路由结果，只加载匹配的工具
    from ..agents.router import get_tools_for_intent
    try:
        if intent and intent.get("agents"):
            tools = get_tools_for_intent(intent["agents"])
            logger.info(f"使用匹配工具: {len(tools)}个, agents={intent['agents']}")
        else:
            tools = []
    except Exception:  # noqa: silent-except 豁免：意图路由失败时退回空工具集（纯 LLM 回答），不中断任务
        if is_civil_persona(persona_id):
            from ..agents.tool_definitions import CIVIL_CODE_TOOLS as tools
        else:
            from ..agents.tool_definitions import ALL_TOOLS as tools

    return messages, final_query, tools

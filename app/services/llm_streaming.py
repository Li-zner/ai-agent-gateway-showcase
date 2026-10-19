"""统一流式原语：节流推送 / 模型链重试 / 空回复兜底 / DFA 安全过滤

抽取自 app/routes/v2.py（2026-09 重构，行为等价纯移动）。
节流规则：首块立即推送，后续 ≥40 字符 或 ≥100ms 冲刷；流尾冲刷残余缓冲。
"""
import time
import asyncio
import uuid
from typing import AsyncIterator, List, Dict

from ..core.config import LLM_FALLBACK_MODEL, LLM_PRIMARY_MODEL
from ..core.metrics import llm_tokens_total, llm_tokens_detail, llm_requests_total
from ..core.logging import setup_logging
from ..core.redis import get_redis
from ..core.stream_utils import stream_llm
from ..core.semantic_cache import SemanticCache
from ..core.safety_filter import get_filter
from ..core.concurrency import spawn
from ..payment.service import deduct_token_cost
from .chat_support import mark_key_result, fallback_message, safety_message
from .reasoning_guard import ReasoningStreamGuard
from .chat_stream_ctx import ChatStreamCtx

logger = setup_logging()

# P1-1 扣费幂等（2026-09-28）：键粒度＝每次上游 LLM 调用（stream_llm 的 bill_uid），
# 绝不能用整轮 request_uid——ReAct 多步与模型降级重试是分别真实发生的费用，
# 按轮去重会把第 2 步之后全判成重复 → 漏扣（钱找不回来，比双扣更严重）。
# 覆盖范围 B-1：只保证钱不双扣；Prometheus 与日 token 计数仍可能双计（观测口径，另单）。
BILL_IDEM_PREFIX = "bill_idem:"
# 24h：上游重投与断连排空都在分钟级，一天后同 uid 再现的可能为 0；键量级＝日调用数
BILL_IDEM_TTL_SECONDS = 86400


async def _claim_bill(bill_uid: str) -> bool:
    """扣费幂等声明：True=首次（可进钱包），False=该 bill_uid 已扣过。

    Redis 不可用 **fail-open**（照常扣费）：fail-closed 会把一次 Redis 抖动放大成
    全网漏扣费，与同目录 `payment/deferred._bump_settle_fail`「不可用按 0 不误杀」
    同口径；拿不到锁时退回的正是今日行为，严格不劣于现状。
    无 bill_uid（agents 非流式路径）同样放行——绝不合成 uid，否则跨请求同名会漏扣。
    """
    if not bill_uid:
        return True
    try:
        r = await get_redis()
        # SET NX 一次写即一次声明，无读-改-写竞争，不需要 Lua
        claimed = await r.set(f"{BILL_IDEM_PREFIX}{bill_uid}", "1",
                              nx=True, ex=BILL_IDEM_TTL_SECONDS)
    except Exception as e:
        logger.warning(f"扣费幂等键不可用（本笔照常扣费，可能重复）: {type(e).__name__}")
        return True
    return bool(claimed)


async def stream_llm_throttled(
    ctx: ChatStreamCtx, api_key: str, model_try: str, llm_messages: List[Dict],
    username: str = "", tools: Dict = None, tool_choice: str = None,
    temperature: float = None, max_tokens: int = None, bill_uid: str = "",
) -> AsyncIterator[tuple]:
    """调用 stream_llm 并按「首块立即 + ≥40字符/≥100ms + 尾冲刷」节流产出事件

    产出 (kind, payload)：
      - ("reasoning", text)  思考文本（已经 ReasoningStreamGuard 过滤：系统词/模型
                             身份/Key 指纹不外露，跨块拼接检测，强指纹整流抑制）
      - ("answer", chunk)    节流后的回答块
      - ("usage", dict)      token 用量（含 bill_uid：本次上游调用的扣费幂等键）
      - ("tool_calls", {"delta": [..]})  工具调用增量

    bill_uid 由调用方生成并透传（P1-1）：断连排空与估算兜底要拿同一个键，
    它们看不到 usage 事件，只能靠调用方把 uid 传进去。
    """
    _stream_buffer = ""
    _first_chunk_time = None
    _last_chunk_time = None
    _reason_guard = None  # 惰性创建：仅出现思考增量的流才需要
    async for _ev in stream_llm(
        api_key, model_try, llm_messages,
        tools=tools, tool_choice=tool_choice, username=username,
        temperature=temperature, max_tokens=max_tokens, bill_uid=bill_uid,
    ):
        if _ev["type"] == "usage":
            yield ("usage", {"prompt_tokens": _ev["prompt_tokens"],
                             "completion_tokens": _ev["completion_tokens"],
                             "bill_uid": _ev.get("bill_uid") or bill_uid})
        elif _ev["type"] == "reasoning":
            if _reason_guard is None:
                _reason_guard = ReasoningStreamGuard(getattr(ctx, "persona_id", ""))
            _safe_text = _reason_guard.feed(_ev["text"])
            if _safe_text:
                yield ("reasoning", _safe_text)
        elif _ev["type"] == "content":
            _chunk = _ev["text"]
            _stream_buffer += _chunk
            _now = time.time()
            if _first_chunk_time is None:
                _first_chunk_time = _now
                _last_chunk_time = _now
                yield ("answer", _chunk)
                _stream_buffer = ""
            elif len(_stream_buffer) >= 40 or (_now - _last_chunk_time >= 0.1 and _stream_buffer):
                yield ("answer", _stream_buffer)
                _stream_buffer = ""
                _last_chunk_time = _now
        elif _ev["type"] == "tool_calls":
            yield ("tool_calls", {"delta": _ev["delta"]})
    # 流尾冲刷：思考守卫按完整行缓冲，最后一段无换行的尾行在此补检放行（防指纹
    # 藏在流尾绕过）；answer 残余缓冲照旧冲刷（原版简单/推荐通道会丢弃 <40 字符）
    if _reason_guard is not None:
        _reason_tail = _reason_guard.flush()
        if _reason_tail:
            yield ("reasoning", _reason_tail)
    if _stream_buffer:
        yield ("answer", _stream_buffer)


def _log_deduct_task_error(task: asyncio.Task) -> None:
    """扣费后台任务的异常认领：不打断对话，但**必须 error 级可告警**。

    2026-09-19 审查 06-payment F7：此前是 warning，而这条路径失败意味着一笔费用
    静默漏计（含 defer_deduction 占位单入库失败——钱已经花了，账没落）。全部扣费
    入口（chat / runner / simple_task / orchestrator / sub_agents）都经
    bill_token_usage 收口到这里，所以只在这一处提级。
    """
    if not task.cancelled() and task.exception():
        logger.error(f"Token扣费后台任务失败（该笔费用未落账，需对账）: "
                     f"{task.exception()}")


async def _claim_file_session(username: str, session_id: str) -> bool:
    """文件内容计费幂等（L2，2026-09-28 主人拍板"同会话只记一次"）：

    True=本会话首次带文件（文件注入部分照常计费），False=已计过（后续轮次
    扣除注入部分再计）。Redis 故障按 True 处理（fail-closed：宁可多计不少计）。
    TTL 24h 覆盖会话生命周期；键过会话即失效，换会话重新计一次符合拍板语义。
    """
    try:
        r = await get_redis()
        return bool(await r.set(f"file_bill_once:{username}:{session_id}", "1",
                                nx=True, ex=86400))
    except Exception:
        return True


def bill_token_usage(usage: dict, username: str, session_id: str, remark: str):
    """日 token 计数 + 钱包扣费（不含 Prometheus 指标，供已自行打点的路径复用）

    2026-09-09 审查 P0：agent 任务路径（runner）原先只打 v2_task 指标、从不扣费
    也不计日 token（"旁路漏扣费"历史教训同类），复用本函数补齐且避免指标双计。
    扣费任务统一走 spawn（强引用防 GC 中途回收 → 静默漏扣，2026-09-07 审查 P2）。

    P1-1（2026-09-28）：进钱包前先做 Redis SETNX 幂等声明，键取 usage 里的
    `bill_uid`（每次上游调用一个，由 stream 侧生成、断连兜底沿用）。claim 放在
    协程内而不是本同步函数里，是为了不动 4 处 agent 直调方的签名（B-1 只管钱）。

    L2（2026-09-28 主人拍板）：usage 里的 `file_ctx_chars`（本轮注入的文件上下文
    字数）参与钱包口径——同会话首次照常全额计，后续轮次扣除注入部分（估算，
    中文≈1字1token）。日 token 配额仍按真实全量计（额度=真实用量，钱包=计费）。
    """
    prompt_tk = usage.get("prompt_tokens", 0)
    completion_tk = usage.get("completion_tokens", 0)
    if not (prompt_tk or completion_tk):
        return
    # 日 token 计数（2026-09-07 审查 P1）：daily_token 限额唯一写入方；
    # 日期用业务日（上海 05:00 切日，与 ensure_chat_allowed 门禁同一口径，L3 统一）
    from ..middleware.rate_limit import update_daily_usage
    from ..core.day_window import quota_day
    total_tk = prompt_tk + completion_tk
    _bill_uid = usage.get("bill_uid") or ""
    _today = quota_day()
    spawn(update_daily_usage(username, _today, inc_request=0, inc_token=total_tk),
          name="daily-token-incr")

    _file_chars = int(usage.get("file_ctx_chars") or 0)

    async def _claim_and_deduct():
        if not await _claim_bill(_bill_uid):
            logger.warning(f"重复扣费已拦截（bill_uid={_bill_uid}, tokens={total_tk}，"
                           f"同一笔费用的第二次声明）")
            return {"deducted": False, "reason": "duplicate_bill"}
        bill_tokens = total_tk
        if _file_chars and session_id:
            if not await _claim_file_session(username, session_id):
                # 本会话已为文件内容计过费：扣掉注入部分再计（只留本轮新增内容）
                bill_tokens = max(0, total_tk - min(_file_chars, prompt_tk))
        return await deduct_token_cost(
            user_id=username, token_count=bill_tokens, session_id=session_id or "",
            remark=remark,
        )

    spawn(_claim_and_deduct(),
          name="token-deduct").add_done_callback(_log_deduct_task_error)


def _estimate_tokens(text: str) -> int:
    """断连估算兜底：拿不到上游 usage 时按已产出文本粗估。

    中文≈1字1token、英文≈4字符1token，混合取 2 字符 1 token 折中。
    ponytail: 粗估偏差可达 ±50%，仅断连兜底用；正常路径始终走真实 usage 事件。
    """
    if not text:
        return 0
    return max(1, len(text) // 2)


def _spawn_drain_bill(agen, username: str, conv_id: str, tag: str,
                      fallback_text: str = "", model: str = "",
                      bill_uid: str = "", file_ctx_chars: int = 0) -> None:
    """断连排空计费（2026-09-09 审查 P1 修复：断连漏扣费）。

    客户端断开时下游生成器收到 GeneratorExit/CancelledError，此刻上游流对象
    （stream_llm → httpx resp）尚未关闭——排空任务接管消费直至上游自然结束，
    拿到真实 usage 事件后按真实值补计费；上游中途失败则按已产出文本估算兜底。
    ponytail: 排空会把上游剩余输出消耗完（费用与用户不断连时一致），换取计量准确；
    spawn 持强引用防 GC 中途回收。

    bill_uid（P1-1 双扣修复）：排空接管的是**同一次上游调用**，必须用同一个幂等键。
    此前正是这条路径双扣——usage 已被主路径消费掉，排空拿不到 usage 便落进估算分支，
    又按估算另起一笔扣费。估算分支没有 usage 事件可依附，uid 只能由调用方传进来。
    """
    async def _drain():
        usage = None
        try:
            async for kind, payload in agen:
                if kind == "usage":
                    usage = payload
        except Exception as e:
            logger.warning(f"{tag} 断连排空中断（{type(e).__name__}），退化估算计费")
            usage = None
        if usage and (usage.get("prompt_tokens") or usage.get("completion_tokens")):
            # L2：文件注入长度并入 usage 携带（不动 record 签名，既有桩透明）
            record_token_usage({**usage, "file_ctx_chars": file_ctx_chars},
                               username, conv_id, model=model)
            logger.info(f"{tag} 断连排空完成，按真实 usage 补计费")
        elif fallback_text:
            est = _estimate_tokens(fallback_text)
            record_token_usage(
                {"prompt_tokens": 0, "completion_tokens": est, "bill_uid": bill_uid},
                username, conv_id,
                remark=f"断连估算补计费（{est} tokens，按已产出文本粗估）")
            logger.info(f"{tag} 上游未给出 usage，按估算补计费 {est} tokens")

    spawn(_drain(), name=f"drain-bill:{tag}")


def record_token_usage(_stream_usage: dict, username: str, conv_id: str,
                       remark: str = "", model: str = "", endpoint: str = "v2_chat"):
    """Token 精细计量 + 扣费（10元/万token，模拟模式）

    Prometheus 指标 + 日 token 计数 + 异步扣费；扣费失败经 done_callback 记 warning，不影响对话主流程。
    2026-09-06 自 chat_react 下沉至本模块：快速通道（answer_via_models）此前不透传
    usage 事件导致主流量漏扣费，现由透传点统一计量，ReAct/快速通道两路径共用。
    remark 供断连估算等特殊计费路径标注口径。
    model 传实际应答模型（2026-09-10 审查 P2：原硬编码主模型名，flash/qwen 降级
    实际发生的 token 全记主模型名下，监控口径失真）；缺省回落主模型名。
    """
    _dm = model or LLM_PRIMARY_MODEL
    prompt_tk = _stream_usage.get("prompt_tokens", 0)
    completion_tk = _stream_usage.get("completion_tokens", 0)
    try:
        from .rag_request_trace import add_token_usage
        add_token_usage(prompt_tk, completion_tk)
    except Exception:  # noqa: silent-except — trace 用量统计失败不影响流式
        pass
    if prompt_tk or completion_tk:
        llm_tokens_total.labels(type='input').inc(prompt_tk)
        llm_tokens_total.labels(type='output').inc(completion_tk)
        llm_tokens_detail.labels(model=_dm, endpoint=endpoint, type='input').inc(prompt_tk)
        llm_tokens_detail.labels(model=_dm, endpoint=endpoint, type='output').inc(completion_tk)
    llm_requests_total.labels(model=_dm, endpoint=endpoint, status='success').inc()
    # file_ctx_chars 经 usage 字典携带（L2）：不扩本函数签名，既有桩/调用方零改动
    bill_token_usage(dict(_stream_usage), username, conv_id,
                     remark=remark or f"AI对话消耗 {prompt_tk + completion_tk} tokens（输入 {prompt_tk} + 输出 {completion_tk}）")


async def answer_via_models(ctx: ChatStreamCtx, llm_messages: List[Dict], tag: str) -> AsyncIterator[tuple]:
    """按 model_try_list 顺序调用模型并把节流事件透传给调用方

    单模型失败标记 Key 后换下一个；全部失败/空回复由调用方兜底。
    产出 (kind, text) 供快速通道渲染 reasoning/answer。
    """
    for _model_try in ctx.model_try_list:
        _result_text = ""
        # 每次上游调用一个幂等键（P1-1）：换模型重试是另一笔真实费用，所以 uid 在
        # 循环内生成；断连排空与估算兜底沿用同一个，拦得住"主路径已扣 + 排空再扣"
        _bill_uid = uuid.uuid4().hex
        _agen = stream_llm_throttled(
            ctx, ctx.api_key, _model_try, llm_messages, username=ctx.username,
            bill_uid=_bill_uid,
        )
        try:
            async for kind, payload in _agen:
                if kind == "reasoning":
                    yield ("reasoning", payload)
                elif kind == "answer":
                    _result_text += payload
                    yield ("answer", payload)
                elif kind == "usage":
                    # L2 同会话文件只计一次：快速通道与 ReAct 路（chat_react:405）
                    # 同契约，file_ctx_chars 经 usage 字典携带（2026-10-07 P1-11）
                    record_token_usage(
                        {**payload, "file_ctx_chars": getattr(ctx, "file_ctx_chars", 0)},
                        ctx.username, ctx.conv_id, model=_model_try)
            if _result_text:
                return
        except (GeneratorExit, asyncio.CancelledError):
            # 客户端断连（2026-09-09 审查 P1 修复：断连漏扣费）：此刻上游流未关闭，
            # 排空任务接管消费拿真实 usage 补计费后原样上抛，不吞取消
            _spawn_drain_bill(_agen, ctx.username, ctx.conv_id,
                              f"fast-path[{_model_try}]", fallback_text=_result_text,
                              model=_model_try, bill_uid=_bill_uid,
                              file_ctx_chars=getattr(ctx, "file_ctx_chars", 0))
            raise
        except Exception as _fast_err:
            await mark_key_result(ctx.api_key, False)
            llm_requests_total.labels(model=_model_try, endpoint='v2_chat', status='error').inc()  # 2026-09-11 审查 P1：告警接线
            # 已流出部分回答就不再换模型（2026-09-10 审查 P2）：换模型重答会与
            # 已发出的半截回答拼接重复；按失败收尾交调用方兜底
            if _result_text:
                logger.warning(f"{tag} {_model_try} 中途失败（已出 {len(_result_text)} 字，不换模型重答）: {_fast_err}")
                return
            if _model_try == ctx.selected_model:
                logger.warning(f"{tag} {ctx.selected_model} 调用失败，降级到 {LLM_FALLBACK_MODEL}: {_fast_err}")
            else:
                logger.warning(f"{tag} {LLM_FALLBACK_MODEL} 也失败: {_fast_err}")
    # 全部模型失败：交由调用方 ensure_answer 兜底


async def ensure_answer(ctx: ChatStreamCtx, result_text: str, fallback_msg: str) -> str:
    """空回复兜底 + 穿透占位；返回最终答案文本"""
    if not result_text:
        result_text = fallback_message(ctx.req)
        logger.warning(f"{fallback_msg}")
        await mark_key_result(ctx.api_key, False)
        # 穿透防护：底层失败 → 写入短 TTL 空值占位（await 确保写入，P0 #11）
        await SemanticCache.set_empty(ctx.req.query, cache_ctx=ctx.cache_ctx, ttl=30)
    return result_text


def apply_safety_filter(text: str) -> str:
    """DFA 敏感词过滤（命中即替换为安全提示）"""
    sf = get_filter()
    if sf.contains_sensitive(text):
        return safety_message(None, sf.safe_message)
    return text

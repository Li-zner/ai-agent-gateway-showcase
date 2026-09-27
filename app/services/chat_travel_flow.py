"""旅行推荐多轮槽位流：澄清轮不跑工具，短回复继承上一轮意图。"""
import time
from typing import AsyncIterator

from ..core.logging import setup_logging
from ..core.memory_manager import PENDING_TRAVEL_TTL_SECONDS, PENDING_TRAVEL_TS_KEY
from ..core.place_extract import extract_place_reply
from ..core.stream_utils import sse
from .conversation_profiles import extract_profile_updates
from .chat_stream_ctx import ChatStreamCtx, finalize_answer
from .rag_request_trace import mark_route

logger = setup_logging()


TRAVEL_AGENTS = ["query_weather", "query_hotel", "query_route", "query_food"]


def travel_intent_result(method: str) -> dict:
    """构造恢复后的旅行推荐意图，供闸门直接收口或继续跑四工具。"""
    return {
        "agents": list(TRAVEL_AGENTS),
        "is_simple": False,
        "is_recommend": True,
        "method": method,
    }


def is_cancel_reply(query: str) -> bool:
    """识别结束当前旅行规划的自然语言回复。"""
    return (query or "").strip().strip("。！？!?，,、 ") in {
        "算了", "不用了", "取消", "不去了", "不规划了", "先不弄了",
    }


def next_missing_travel_slot(state: dict) -> str:
    """返回下一个必填槽；目的地优先，其次出发地。"""
    if not str(state.get("destination") or "").strip():
        return "destination"
    if not str(state.get("departure") or "").strip():
        return "departure"
    return ""


def build_travel_query(state: dict) -> str:
    """把已确认目的地/出发地合成规范查询，工具参数同源且不重复澄清。"""
    destination = str(state.get("destination") or "").strip()
    departure = str(state.get("departure") or "").strip()
    details = [
        str(value).strip() for value in (
            state.get("days"), state.get("travelers"), state.get("budget"),
        ) if str(value or "").strip()
    ]
    if destination:
        query = f"从{departure}出发，去{destination}旅游。"
        if details:
            query += f"已确认：{'、'.join(details)}。"
        return query + "请覆盖天气、交通、景点、美食和住宿。"
    return (
        f"从{departure}出发安排旅行。请覆盖天气、交通、景点、美食和住宿，"
        "并继续遵守上一轮中的其他偏好。"
    )


def travel_clarification_message(state: dict) -> str:
    """生成不含内部实现细节的槽位追问。"""
    if state.get("missing_slot") == "destination":
        return "可以，请先告诉我你想去哪个城市或地区旅游。"
    destination = str(state.get("destination") or "").strip()
    return (
        f"可以，去{destination}旅游的话，请先告诉我从哪里出发，"
        "我再把天气、交通、景点、美食和住宿一起安排好。"
    )


async def get_pending_travel(ctx: ChatStreamCtx) -> dict:
    """读取会话待补槽；测试桩或依赖故障时按无状态继续。"""
    getter = getattr(ctx.mm, "get_pending_travel", None)
    if not callable(getter):
        return {}
    return await getter()


async def get_stored_profile(ctx: ChatStreamCtx) -> dict:
    """读取结构化画像；依赖故障时按无画像继续，不阻断路由。"""
    getter = getattr(ctx.mm, "get_conversation_profile", None)
    if not callable(getter):
        return {}
    try:
        return await getter()
    except Exception as e:
        # 返回值仍是空画像（不阻断路由），但必须留痕：画像读不到会让"重新规划"
        # 静默丢掉上一轮的出发地/目的地，没有日志就只能靠用户报障反推（09-22 P2）
        logger.warning(f"会话画像读取失败（按无画像继续）: {type(e).__name__}: {e}")
        return {}


async def build_travel_state(ctx: ChatStreamCtx) -> dict:
    """合并本轮显式槽位与服务端画像，供无地点重规划恢复上下文。"""
    stored = await get_stored_profile(ctx)
    updates = extract_profile_updates(ctx.user_query or "")
    merged = {**stored, **{key: value for key, value in updates.items() if value}}
    return {
        "kind": "travel",
        "original_query": ctx.req.query or ctx.user_query,
        "destination": merged.get("destination", ""),
        "departure": merged.get("origin", ""),
        "days": merged.get("days", ""),
        "travelers": merged.get("travelers", ""),
        "budget": merged.get("budget", ""),
    }


async def persist_travel_slot(ctx: ChatStreamCtx, slot: str, value: str) -> None:
    """把确认后的旅行槽位写入会话画像；画像失败不影响本轮工具查询。

    2026-09-28 批1 长尾 P2：docstring 一直这么承诺，但裸 await 在 pool.acquire
    抛错时会冒泡到 chat_stream_core 降级成无工具裸聊——兑现承诺，失败只记日志。
    """
    updater = getattr(ctx.mm, "update_profile_fields", None)
    if not callable(updater):
        return
    try:
        await updater({slot: value})
    except Exception:
        logger.exception(f"旅行槽位写画像失败（不影响本轮查询）: slot={slot}")


def _pending_is_fresh(pending: dict) -> bool:
    """待补槽是否仍在有效期内（2026-09-22 审阅 P2）。

    Redis 侧 TTL 已降到分钟级，但那只管键的淘汰：旧版本写入的无时间戳键、
    回填/测试桩等路径都可能在键仍在时把隔日状态当本轮上下文。缺 saved_at 一律
    按不新鲜处理（宁可让用户重说一次出发地，也不把答案写到昨天的行程上）。
    """
    try:
        saved_at = float(pending.get(PENDING_TRAVEL_TS_KEY) or 0.0)
    except (TypeError, ValueError):
        return False
    now = time.time()  # 只读一次时钟：两次读之间跨分钟的边界会判成"未来"或"超期"
    return 0.0 < saved_at <= now and (now - saved_at) <= PENDING_TRAVEL_TTL_SECONDS


async def apply_travel_memory(ctx: ChatStreamCtx, intent_result: dict) -> dict:
    """用上一轮待补槽解释本轮短回复；显式新请求优先于旧槽位。"""
    pending = await get_pending_travel(ctx)
    if not pending:
        return intent_result
    if not _pending_is_fresh(pending):
        ctx.travel_pending_clear = True
        return intent_result
    query = (ctx.user_query or "").strip()
    if is_cancel_reply(query):
        ctx.travel_pending_clear = True
        return intent_result
    # 用户明确发起新一轮旅游推荐时，不把本轮当成旧槽位的补充。
    if intent_result.get("is_recommend"):
        ctx.travel_pending_clear = True
        return intent_result
    # 明确问天气/法律等其他意图时保留旧槽位，等用户下次回来继续。
    if intent_result.get("agents"):
        return intent_result

    place = extract_place_reply(query)
    if place:
        slot = pending.get("missing_slot")
        if slot == "destination":
            pending["destination"] = place
            await persist_travel_slot(ctx, "destination", place)
        elif slot == "departure":
            pending["departure"] = place
            await persist_travel_slot(ctx, "origin", place)
        next_slot = next_missing_travel_slot(pending)
        pending["missing_slot"] = next_slot
        ctx.travel_pending = pending
        if next_slot:
            ctx.travel_waiting_slot = True
            return travel_intent_result("travel_slot_memory")
        ctx.user_query = build_travel_query(pending)
        ctx.travel_pending_clear = True
        return travel_intent_result("travel_slot_memory")

    # 未识别为地点时保持推荐通道，继续问缺失槽位，不能掉回纯聊天裸答。
    ctx.travel_pending = pending
    ctx.travel_waiting_slot = True
    return travel_intent_result("travel_slot_reask")


async def travel_clarification_gate(ctx: ChatStreamCtx) -> AsyncIterator[str]:
    """旅行缺槽时直接澄清：保存待补状态后收口，不进入任何工具通道。"""
    if not ctx.travel_waiting_slot or not ctx.travel_pending:
        return
    setter = getattr(ctx.mm, "set_pending_travel", None)
    if callable(setter):
        await setter(ctx.travel_pending)
    # 本轮写入的是最新待补状态，不能被 finalize 当成"已消费"清掉。
    ctx.travel_pending_clear = False
    message = travel_clarification_message(ctx.travel_pending)
    mark_route("travel_clarification")
    yield sse("answer_complete", message) + "data: [DONE]\n\n"
    # C-F11 不变式（chat_react._finalize_partial_answer 同一收口顺序，09-22 审阅 P1）：
    # 内容已送达就先置标志，再 finalize。原先先 finalize 后置标志，台账一抛错
    # 上层 except 就拿不到 saved_normally，会在 [DONE] 之后降级输出第二段答案
    # 并二次计费（chat_stream_core._fallback_events）。
    ctx.saved_normally = True
    ctx.finished = True
    try:
        await finalize_answer(ctx, message, write_cache=False)
    except Exception as fin_err:
        logger.warning(f"旅行澄清收尾失败（内容已送达，不影响用户）: {fin_err}")

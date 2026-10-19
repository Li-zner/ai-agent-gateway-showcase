"""会话级用户画像：提取、读写、天气快照与历史压缩。"""
import json
import re
from datetime import datetime, timezone
from typing import Any

from ..core.logging import setup_logging
from ..core.place_extract import extract_departure, extract_destination


logger = setup_logging()

PROFILE_TEXT_FIELDS = (
    "name", "travelers", "origin", "destination", "budget", "days", "notes",
)
WEATHER_CITY_FIELD = "weather_city"
WEATHER_UPDATED_FIELD = "weather_updated_at"
WEATHER_FORECAST_FIELD = "weather_forecast"
MAX_WEATHER_DAYS = 3
USER_PREVIEW_CHARS = 100

# 批1 B-P1-4（2026-09-28）：主人拍板画像不记姓名——原先 _NAME_RE 含裸「我是」「姓名」
# 且冒号可选，实测「我是来看法律的」抽成 name="来看法律的"。PROFILE_TEXT_FIELDS 里的
# "name" 保留，只为存量行归一化与中控台展示，抽取支线不再新增。
# 旅行上下文闸：命中才尝试抽目的地（否则"我叫张三"这类裸句子会被误当目的地）。
# 原先只列中文词——EN/中文混查（"traveling ... to 盐城"）整个闸不命中，
# extract_destination 根本不被调用，recommend 路径卡在"想去哪个城市"
# （2026-10-15 活体复现）。补英文旅行词并忽略大小写；这只是布尔上下文闸，
# 英文子串误命中（plan↮explain）最多触发一次无害的抽名尝试，抽不到就返回空。
_TRAVEL_CONTEXT_RE = re.compile(
    r"旅游|旅行|游玩|度假|自由行|攻略|推荐|规划|行程|去|到|前往|出发"
    r"|travel|trip|tour|vacation|itinerary|plan|visit|holiday"
    r"|transportation|accommodation|flight",
    re.IGNORECASE,
)
_COUNT_PATTERN = r"[0-9零一二两三四五六七八九十百千万]+"
_PEOPLE_RE = re.compile(
    rf"({_COUNT_PATTERN})\s*个?人|一家\s*({_COUNT_PATTERN})\s*口"
    rf"|({_COUNT_PATTERN})大\s*({_COUNT_PATTERN})小"
)
_CN_DIGITS = {
    "零": "0", "一": "1", "二": "2", "两": "2", "三": "3", "四": "4",
    "五": "5", "六": "6", "七": "7", "八": "8", "九": "9",
}
_DAYS_RE = re.compile(r"(\d+)\s*天")
_BUDGET_RE = re.compile(
    r"预算(?:是|为|大约|大概|控制在|在|改成|改为|调整为|变成|调成|设为|设置为)?\s*"
    rf"({_COUNT_PATTERN}(?:元|块|万)?)"
)


def empty_conversation_profile() -> dict:
    """返回前端可直接绑定的一致空画像。"""
    profile = {field: "" for field in PROFILE_TEXT_FIELDS}
    profile[WEATHER_FORECAST_FIELD] = []
    profile[WEATHER_CITY_FIELD] = ""
    profile[WEATHER_UPDATED_FIELD] = ""
    return profile


def normalize_conversation_profile(raw: Any) -> dict:
    """清洗 DB/API 画像，防止旧数据或异常 JSON 污染前端协议。"""
    data = raw if isinstance(raw, dict) else {}
    profile = empty_conversation_profile()
    for field in PROFILE_TEXT_FIELDS:
        value = data.get(field)
        if value is not None:
            profile[field] = str(value)[:1000]
    profile[WEATHER_CITY_FIELD] = str(data.get(WEATHER_CITY_FIELD) or "")[:64]
    profile[WEATHER_UPDATED_FIELD] = str(data.get(WEATHER_UPDATED_FIELD) or "")[:64]
    forecast = data.get(WEATHER_FORECAST_FIELD)
    if isinstance(forecast, list):
        profile[WEATHER_FORECAST_FIELD] = [
            _normalize_weather_day(item) for item in forecast[:MAX_WEATHER_DAYS]
            if isinstance(item, dict)
        ]
    return profile


def _normalize_weather_day(item: dict) -> dict:
    """天气卡片字段统一短字符串，避免把第三方响应原样塞进画像。"""
    return {
        "date": str(item.get("date") or "")[:16],
        "day_weather": str(item.get("day_weather") or "")[:32],
        "night_weather": str(item.get("night_weather") or "")[:32],
        "day_temp": str(item.get("day_temp") or "")[:16],
        "night_temp": str(item.get("night_temp") or "")[:16],
        "day_wind": str(item.get("day_wind") or "")[:32],
        "night_wind": str(item.get("night_wind") or "")[:32],
    }


def _normalize_count(value: str) -> str:
    """把常见中文人数归一成阿拉伯数字，未知写法保持原样。"""
    if value.isdigit():
        return value
    return _CN_DIGITS.get(value, value)


def weather_snapshot_from_tool(result: Any) -> dict:
    """把 query_weather 的真实结果转成画像字段；错误结果返回空更新。"""
    if not isinstance(result, dict) or result.get("error"):
        return {}
    forecast = result.get("forecast")
    if not isinstance(forecast, list):
        return {}
    days = [
        _normalize_weather_day(item) for item in forecast[:MAX_WEATHER_DAYS]
        if isinstance(item, dict)
    ]
    if not days:
        return {}
    return {
        WEATHER_CITY_FIELD: str(result.get("city") or "")[:64],
        WEATHER_UPDATED_FIELD: datetime.now(timezone.utc).isoformat(),
        WEATHER_FORECAST_FIELD: days,
    }


async def persist_weather_snapshot(mm: Any, result: Any) -> bool:
    """把真实天气工具结果写入当前会话画像；失败不阻断主回答。"""
    updates = weather_snapshot_from_tool(result)
    updater = getattr(mm, "update_profile_fields", None)
    if not updates or not callable(updater):
        return False
    try:
        await updater(updates)
        return True
    except Exception as e:
        # 画像属于旁路增强，不能因为数据库抖动影响已经拿到的天气回答。
        logger.warning(f"天气画像写入失败（不阻断回答）: {type(e).__name__}")
        return False


def extract_profile_updates(user_query: str) -> dict:
    """从单轮用户问题提取画像字段；无上下文裸城市不误判为目的地。

    批1 B-P1-4（2026-09-28）：不再提取姓名，见模块内 _NAME_RE 删除处的说明。
    """
    query = (user_query or "").strip()
    if not query:
        return {}
    updates = {}
    people_match = _PEOPLE_RE.search(query)
    if people_match:
        groups = [g for g in people_match.groups() if g]
        if people_match.group(0).startswith("一家"):
            updates["travelers"] = f"一家{_normalize_count(groups[0])}口"
        elif "大" in people_match.group(0):
            updates["travelers"] = (
                f"{_normalize_count(groups[0])}大{_normalize_count(groups[1])}小"
            )
        else:
            updates["travelers"] = f"{_normalize_count(groups[0])}人"
    origin = extract_departure(query)
    if origin:
        updates["origin"] = origin
    if _TRAVEL_CONTEXT_RE.search(query):
        destination = extract_destination(query)
        if destination:
            updates["destination"] = destination
    days_match = _DAYS_RE.search(query)
    if days_match:
        updates["days"] = f"{days_match.group(1)}天"
    budget_match = _BUDGET_RE.search(query)
    if budget_match:
        updates["budget"] = budget_match.group(1)
    return updates


def _row_profile(row: Any) -> dict:
    """解析 asyncpg 行的 profile JSONB，并补齐协议字段。"""
    raw = row["profile"] if row and "profile" in row else {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = {}
    return normalize_conversation_profile(raw)


async def get_conversation_profile(conn, user_id: str, conversation_id: str) -> dict:
    """读取单会话画像；不存在返回空画像，前端无需处理 404。"""
    row = await conn.fetchrow(
        "SELECT profile, message_count, created_at, updated_at "
        "FROM conversation_profiles WHERE user_id=$1 AND conversation_id=$2",
        user_id, conversation_id,
    )
    profile = _row_profile(row)
    profile["message_count"] = int(row["message_count"]) if row else 0
    return profile


def _normalize_profile_updates(updates: Any) -> dict:
    """把 PATCH 传入的字段清洗成"可直接合并"的增量（写前归一/截断）。

    与 normalize_conversation_profile 的区别是关键：这里**只保留调用方显式传
    的键**，缺的键不能补默认值——否则整列写回会把并发方刚写的字段清空。
    value 为 None 表示"清空该字段"，原样保留（jsonb 的 || 会把显式 null 落库，
    读回时再归一成空串/空数组，协议不变）。
    """
    data = updates if isinstance(updates, dict) else {}
    payload: dict = {}
    for key, value in data.items():
        if key in PROFILE_TEXT_FIELDS:
            payload[key] = "" if value is None else str(value)[:1000]
        elif key in (WEATHER_CITY_FIELD, WEATHER_UPDATED_FIELD):
            payload[key] = "" if value is None else str(value)[:64]
        elif key == WEATHER_FORECAST_FIELD:
            if value is None:
                payload[key] = []
            elif isinstance(value, list):
                payload[key] = [
                    _normalize_weather_day(item)
                    for item in value[:MAX_WEATHER_DAYS] if isinstance(item, dict)
                ]
    return payload


async def patch_conversation_profile(
    conn, user_id: str, conversation_id: str, updates: dict,
) -> dict:
    """创建或局部更新画像；显式 null 用于删除单字段。

    2026-09-22 审查 P1：原实现"先读整列→Python 合并→SET profile = EXCLUDED"
    与主链 memory_manager._upsert_conversation_profile 的
    `profile || EXCLUDED.profile` 并发互踩——读-改-写窗口内主链刚写的字段会被
    本函数用旧快照整列盖掉（反之亦然），双方都丢更新。现在两侧走同一条 jsonb
    合并单语句：合并发生在数据库内，无读-改-写窗口。归一/截断仍在写前完成，
    读回时再整体 normalize 一次兜住历史脏数据。归属维度 user_id 未动。
    """
    row = await conn.fetchrow(
        "INSERT INTO conversation_profiles "
        "(user_id, conversation_id, profile, message_count) "
        "VALUES ($1, $2, $3::jsonb, 0) "
        "ON CONFLICT (user_id, conversation_id) DO UPDATE "
        "SET profile = conversation_profiles.profile || EXCLUDED.profile, "
        "updated_at = NOW() "
        "RETURNING profile, message_count, created_at, updated_at",
        user_id, conversation_id,
        json.dumps(_normalize_profile_updates(updates), ensure_ascii=False),
    )
    result = _row_profile(row)
    result["message_count"] = int(row["message_count"]) if row else 0
    return result


async def delete_conversation_profile(
    conn, user_id: str, conversation_id: str,
) -> bool:
    """删除单会话画像，返回是否确实删到记录。"""
    result = await conn.execute(
        "DELETE FROM conversation_profiles "
        "WHERE user_id=$1 AND conversation_id=$2",
        user_id, conversation_id,
    )
    return bool(result and not result.endswith(" 0"))

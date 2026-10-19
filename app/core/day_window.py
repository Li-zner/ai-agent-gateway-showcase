"""统一"业务日"口径：Asia/Shanghai 凌晨 05:00 切日（2026-09-28 主人拍板，L3）。

此前 daily_req/daily_token、短信日键、订单号日期段、对账/今日统计窗口全部按
UTC 零点切日——对中文用户额度在北京 08:00 重置，"今日/明日"语义失真。现在
全仓"日"只有一个定义：**本地（上海）05:00 前算前一天**。选 05:00 是主人指定
（凌晨低峰，且比 0 点多覆盖一晚上的访问计入前一天报表）。

新增日键/日窗口一律 import 本模块，不要再地写 now().strftime——两处定义漂移
就是这次要修的病。时区固定 Asia/Shanghai（部署面向中文用户；换市场改一处）。
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Asia/Shanghai")
FLIP_HOUR = 5  # 切日时刻（本地小时）


def _local_now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(_TZ)


def quota_day(now: datetime | None = None) -> str:
    """业务日（YYYY-MM-DD）：本地 05:00 前归属前一天。Redis 日键后缀用它。"""
    local = _local_now(now)
    if local.hour < FLIP_HOUR:
        local -= timedelta(days=1)
    return local.strftime("%Y-%m-%d")


def quota_day_compact(now: datetime | None = None) -> str:
    """同 quota_day 的紧凑版（YYYYMMDD），订单号日期段等短键用。"""
    return quota_day(now).replace("-", "")


def day_window_utc_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    """当前业务日的 [起点, 次日起点)，返回 UTC datetime（SQL 范围查询直接可用）。"""
    local = _local_now(now)
    start_local = local.replace(hour=FLIP_HOUR, minute=0, second=0, microsecond=0)
    if local < start_local:
        start_local -= timedelta(days=1)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def seconds_until_flip(now: datetime | None = None) -> int:
    """距下一次切日的秒数（Redis 日键 TTL 对齐用，正值、<86400）。"""
    _start, end = day_window_utc_bounds(now)
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return max(1, int((end - now_utc).total_seconds()))

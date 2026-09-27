"""单 IP 短信日额度的发送前预检（2026-09-22 审查 P2 自 phone.py 门禁拆出）。

口径：额度**只在短信确实发出去之后**由 phone._check_sms_rate_ip 用 SMS_IP_DAY_LUA
落账（INCR + 首次 EXPIRE + 超限即 DECR 回滚）。本模块只做只读预检。

为什么预检和落账要分开：把 INCR 放在发送之前，per-phone 分钟冷却抛 429、上游
故障抛 503 时额度已经白扣了，用户重试几次就被"今日上限"锁死；反过来只留落账、
不留预检，超额 IP 每来一个请求仍会真的发出一条短信（计费轰炸面又回来了）。
"""
from fastapi import HTTPException

# 单 IP 日发送上限（09-09 P1）：per-phone 限流可被换号绕过，本键兜底单 IP 日总量
SMS_IP_DAY_LIMIT = 20


def ip_day_key(ip: str) -> str:
    """IP 日计数键名；预检与落账共用同一个构造口，防两处拼写漂移。

    2026-09-28 批1 长尾 P2：键带自然日后缀——原键无日期、TTL 只在首次 INCR 置 86400，
    实为"自首条滚动 24h"，与 429 文案"明日再试"不符；按自然日切键后文案成立。
    日期用 UTC，与 daily_token 门禁同一日切口径。
    """
    from datetime import datetime, timezone
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"phone_sms_ip_day:{ip}:{day}"


async def assert_ip_day_headroom(ip: str) -> None:
    """IP 日额度已满则直接 429，**不**动计数器。

    只读 GET 与后面的落账之间存在并发窗口，最多放行"同时在飞"的请求数溢出；
    落账脚本里的"INCR 后超限即 DECR 回滚"保证溢出部分不落进计数，不累积漂移。
    键不存在或值脏（非数字）都按未超限处理，交给落账脚本裁决。
    """
    from ..core.redis import get_redis
    r = await get_redis()
    used = await r.get(ip_day_key(ip))
    try:
        exhausted = int(used) >= SMS_IP_DAY_LIMIT
    except (TypeError, ValueError):
        exhausted = False
    if exhausted:
        raise HTTPException(429, "今日发送次数已达上限，请明日再试")

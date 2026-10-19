"""短信限流与验证码校验原语（2026-09-29 自 phone.py 拆出防 600 行门禁）。

只做 Redis Lua 原语与 HTTPException 映射，不含路由；phone.py 保留路由与业务编排。
Redis 连接由调用方注入（`r` 首参）：测试桩 phone.get_redis 即可全链生效，
与拆分前行为一致。
"""
from fastapi import HTTPException

from ..core.logging import setup_logging
from ..services.sms_ip_quota import (
    IP_DAY_DENIED_MSG, SMS_IP_DAY_LIMIT, PHONE_DAY_DENIED_MSG,
    SMS_PHONE_DAY_LIMIT, ip_day_key, phone_day_key, settle_day_quota,
)
from .sms_lua import SMS_IP_DAY_LUA, SMS_SEND_LUA, VERIFY_CODE_LUA
from ..core.config import (
    SMS_SEND_MIN_INTERVAL, SMS_SEND_HOUR_LIMIT,
    SMS_ATTEMPT_LIMIT, SMS_ATTEMPT_LOCK_SECONDS,
)

logger = setup_logging()


async def check_sms_rate(r, phone: str):
    """发送频率限制：同一手机号 1 分钟 1 次、1 小时 5 次（单脚本原子，AUTH-1 收口）"""
    verdict = await r.eval(
        SMS_SEND_LUA, 2,
        f"phone_sms_min:{phone}",
        f"phone_sms_hour:{phone}",
        str(SMS_SEND_MIN_INTERVAL), str(SMS_SEND_HOUR_LIMIT), "3600",
    )
    if verdict == 0:
        raise HTTPException(429, "短信发送次数已达上限，请稍后再试")
    if verdict == -1:
        raise HTTPException(429, "发送太频繁，请 1 分钟后再试")


async def check_sms_rate_ip(r, ip: str):
    """IP 维度日上限**落账**（2026-09-09 审查 P1）：换号绕过 per-phone 限流时的
    计费兜底；单脚本原子化同 AUTH-1。

    2026-09-22 审查 P2：调用点从"发送前"移到"短信确实发出去之后"——原来
    per-phone 分钟冷却抛 429、上游故障抛 503 时 IP 额度已经白扣，用户一条短信
    没收到却耗尽当日额度。防轰炸语义没丢：发送前先走 services.assert_ip_day_headroom
    只读预检（超额直接拒，不触发下发），落账仍是脚本内 INCR→超限 DECR 回滚，
    等价"先查后增"却把并发窗口收进脚本——预检与落账之间最多溢出"同时在飞"
    的条数，且溢出部分被回滚、不会累积。

    批1 A-P1-2：落账动作抽成维度无关的 `settle_day_quota`，单号维度共用同一条
    路径，两个上限不会各自漂移出第三种语义（脚本正文仍在 routes/sms_lua，此处传入）。
    """
    await settle_day_quota(r, SMS_IP_DAY_LUA, ip_day_key(ip),
                           SMS_IP_DAY_LIMIT, IP_DAY_DENIED_MSG)


async def verify_phone_code(r, phone: str, code: str):
    """原子校验验证码（含错误次数防爆破）；成功时消费验证码与错误计数"""
    status = await r.eval(
        VERIFY_CODE_LUA,
        3,
        f"phone_code_lock:{phone}",
        f"phone_code:{phone}",
        f"phone_code_attempts:{phone}",
        str(code),
        SMS_ATTEMPT_LIMIT,
        SMS_ATTEMPT_LOCK_SECONDS,
    )
    if status == "locked":
        raise HTTPException(429, "验证码错误次数过多，已锁定 15 分钟")
    if status == "expired":
        raise HTTPException(400, "验证码已过期，请重新发送")
    if status == "invalid":
        raise HTTPException(400, "验证码错误")
    if status != "ok":
        raise HTTPException(503, "验证码服务暂不可用，请稍后重试")


async def phone_day_settle(r, phone: str):
    """单号日额度落账（发送成功后调用，失败路径不扣）"""
    await settle_day_quota(r, SMS_IP_DAY_LUA, phone_day_key(phone),
                           SMS_PHONE_DAY_LIMIT, PHONE_DAY_DENIED_MSG)

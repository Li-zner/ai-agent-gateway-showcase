"""短信日额度：单 IP 与单手机号两个维度的发送前预检 + 发送后落账。

口径（2026-09-22 审查 P2 自 phone.py 拆出；2026-09-28 批1 A-P1-2 扩到单号维度）：
额度**只在短信确实发出去之后**由 phone.py 用 SMS_IP_DAY_LUA 落账（INCR + 首次
EXPIRE + 超限即 DECR 回滚）。本模块的 `assert_*_headroom` 只做只读预检。

为什么预检和落账要分开：把 INCR 放在发送之前，per-phone 分钟冷却抛 429、上游
故障抛 503 时额度已经白扣了，用户重试几次就被"今日上限"锁死；反过来只留落账、
不留预检，超额 IP 每来一个请求仍会真的发出一条短信（计费轰炸面又回来了）。

为什么日额度不落进 config.py 而留在这里：与既有 `SMS_IP_DAY_LIMIT` 同一处定义，
两个维度的上限口径共用一个真值源，别散到两处。
"""
from fastapi import HTTPException

# 单 IP 日发送上限（09-09 P1）：per-phone 限流可被换号绕过，本键兜底单 IP 日总量
SMS_IP_DAY_LIMIT = 20
# 单手机号日发送上限（批1 A-P1-2，2026-09-28 主人拍板 3 条）：换 IP 换不出第三条
SMS_PHONE_DAY_LIMIT = 3
# 文案按维度各一份：预检与落账两处共用，防同一维度冒出两种说法。
# 2026-09-28 L3：键按 UTC 自然日切（对齐 daily_token 既有口径），对中文用户实际
# 在北京 08:00 重置——文案不再承诺"明日"（凌晨被限的访客 5 小时就解锁，旧文案
# 失真）。键层是否整体切 +8 时区待主人拍板，文案先改为不做时间承诺。
IP_DAY_DENIED_MSG = "今日发送次数已达每日上限，请稍后再试"
PHONE_DAY_DENIED_MSG = "今日验证码发送次数已达每日上限，请稍后再试"

_DAY_LUA_TTL_SECONDS = "86400"


def _quota_day() -> str:
    """日切口径：业务日（上海 05:00 切日，day_window 单一真相源，L3 统一）。"""
    from ..core.day_window import quota_day_compact
    return quota_day_compact()


def ip_bucket(ip: str) -> str:
    """把访客地址归并成配额桶：IPv4 → /24，IPv6 → /64（批1 A-P1-3）。

    不归并＝拿精确地址作键：IPv6 访客在一个 /64 里有 2^64 个地址可换，v4 也能换
    /24 内的任一地址，20 条/日的兜底形同虚设（09-28 在 Redis 里看到的真实访客就是
    `240e:` 开头的 IPv6）。桶粒度是取舍：太粗（/32、v4 整段）会把同一 NAT 下的
    不同真人合并限速，太细则绕过成本仍为零。

    解析不了的值（"unknown"、Unix socket 路径、脏转发头）**原样返回**：它们本就
    不代表一个真实对端，硬归并会把不同访客塞进同一个桶。
    """
    import ipaddress

    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return ip
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped          # ::ffff:1.2.3.4 按 v4 口径归并
    prefix = 24 if addr.version == 4 else 64
    net = ipaddress.ip_network(f"{addr}/{prefix}", strict=False)
    return str(net)                      # 形如 "1.2.3.0/24" / "240e:1:2:3::/64"


def ip_day_key(ip: str) -> str:
    """IP 日计数键名；预检与落账共用同一个构造口，防两处拼写漂移。

    2026-09-28 批1 长尾 P2：键带自然日后缀——原键无日期、TTL 只在首次 INCR 置 86400，
    实为"自首条滚动 24h"，与 429 文案"明日再试"不符；按自然日切键后文案成立。
    批1 A-P1-3：中段地址改走 `ip_bucket` 网段归并，键里保留前缀长度避免 v4/v6 混淆。
    """
    return f"phone_sms_ip_day:{ip_bucket(ip)}:{_quota_day()}"


def phone_day_key(phone: str) -> str:
    """单手机号日计数键名（批1 A-P1-2）。

    与 `ip_day_key` 同一日切口径同一构造纪律：预检与落账都走这里，键名不散抄。
    """
    return f"phone_sms_day:{phone}:{_quota_day()}"


async def _day_headroom(r, key: str, limit: int) -> bool:
    """只读判断当日额度是否已耗尽。

    键不存在或值脏（非数字）都按未超限处理，交给落账脚本裁决。
    """
    try:
        return int(await r.get(key)) >= limit
    except (TypeError, ValueError):
        return False


async def assert_ip_day_headroom(ip: str) -> None:
    """IP 日额度已满则直接 429，**不**动计数器。

    只读 GET 与后面的落账之间存在并发窗口，最多放行"同时在飞"的请求数溢出；
    落账脚本里的"INCR 后超限即 DECR 回滚"保证溢出部分不落进计数，不累积漂移。
    """
    from ..core.redis import get_redis
    r = await get_redis()
    if await _day_headroom(r, ip_day_key(ip), SMS_IP_DAY_LIMIT):
        raise HTTPException(429, IP_DAY_DENIED_MSG)


async def assert_phone_day_headroom(phone: str) -> None:
    """单手机号日额度预检（批1 A-P1-2）：满 3 条即 429，不触发下发也不动计数。"""
    from ..core.redis import get_redis
    r = await get_redis()
    if await _day_headroom(r, phone_day_key(phone), SMS_PHONE_DAY_LIMIT):
        raise HTTPException(429, PHONE_DAY_DENIED_MSG)


async def settle_day_quota(r, script: str, key: str, limit: int, denied_msg: str) -> None:
    """日额度落账（单脚本原子：INCR + 首次 EXPIRE + 超限 DECR 回滚）。

    `script` 由调用方传入脚本正文：Lua 常量住在 `app/routes/sms_lua.py`（09-20 为躲
    phone.py 600 行门禁外移），services 侧不反向 import routes，按名取用即可。
    维度无关，IP 与单号共用这一条落账路径，两个上限不会各自漂移出第三种语义。
    """
    verdict = await r.eval(script, 1, key, str(limit), _DAY_LUA_TTL_SECONDS)
    if verdict == 0:
        raise HTTPException(429, denied_msg)

"""
角色分级限流（QPS / 并发 / 日请求 / 日 Token）

设计要点（2026-08-18 加固）：
- QPS 滑动窗口：Redis 服务端 TIME 决定窗口（P1 #14 摆脱客户端时钟漂移），Lua 原子计数。
- 并发控制：Redis Hash + token 租约，释放/续期必须校验持有者；长请求后台续租。
- 日用量：Lua 原子 INCRBY 双 Key + 无条件 TTL（P1 #3/#15 防计数丢失 / Key 永不过期）。
- Redis 异常：Fail-open（放行并记 Error 日志），保障业务可用性优先（P1 #5）。
  放行不是静默行为：每次 fail-open 打 rate_limit_failopen_total{gate}，
  配套 alert.rules.yml 规则 26 RateLimitFailOpen（2026-09-19 审查 core P2-6）。
"""
import uuid

from ..core.metrics import rate_limit_failopen_total
from ..core.redis import get_redis
from ..core.logging import setup_logging
from ..core.config import (
    ADMIN_QPS_LIMIT, ADMIN_DAILY_REQ, ADMIN_DAILY_TOKEN, ADMIN_CONCURRENT,
    USER_QPS_LIMIT, USER_DAILY_REQ, USER_DAILY_TOKEN, USER_CONCURRENT,
)

logger = setup_logging()

# ============================================================
# 角色分级限流配置（统一从 core.config 读取，P2 #18 消除 os.getenv 散落）
# ============================================================
_ROLE_LIMITS = {
    "admin": {
        "qps": ADMIN_QPS_LIMIT,
        "daily_req": ADMIN_DAILY_REQ,
        "daily_token": ADMIN_DAILY_TOKEN,
        "concurrent": ADMIN_CONCURRENT,
    },
    "user": {
        "qps": USER_QPS_LIMIT,
        "daily_req": USER_DAILY_REQ,
        "daily_token": USER_DAILY_TOKEN,
        "concurrent": USER_CONCURRENT,
    },
}

_DEFAULT_LIMITS = _ROLE_LIMITS["user"]


def _get_limits(role: str = "user") -> dict:
    """直接返回常量引用，避免每次调用重建字典（P2 #6）"""
    return _ROLE_LIMITS.get(role, _DEFAULT_LIMITS)


# ============================================================
# QPS 滑动窗口 Lua
# ============================================================
# 原理：维护"当前秒"与"前一秒"两个计数器，估算 = 前窗口×(1-已过比例) + 当前窗口。
# 时间：用 Redis 服务端 TIME（高精度），彻底抛弃客户端时间戳（P1 #14/#32）。
# 优化：首次创建 key 才 EXPIRE，后续 INCR 不续期（窗口仅 1-2 秒，P2 #23）。
_QPS_COUNTER_LUA = """
local limit = tonumber(ARGV[1])
local prefix = ARGV[2]
-- 可选第 4 参：离线单测注入确定性时钟；生产传空串 → 改用 Redis 服务端 TIME（P1 #14 去客户端时钟漂移）
local now_arg = ARGV[3]
local now_sec
if now_arg ~= nil and now_arg ~= '' then
    now_sec = tonumber(now_arg)
else
    local now = redis.call('TIME')
    now_sec = tonumber(now[1]) + tonumber(now[2])/1000000
end
local curr_sec = math.floor(now_sec)
local fraction = now_sec - curr_sec
local curr_key = prefix .. curr_sec
local prev_key = prefix .. (curr_sec - 1)
local curr = tonumber(redis.call('GET', curr_key) or 0)
local prev = tonumber(redis.call('GET', prev_key) or 0)
local estimate = prev * (1 - fraction) + curr
if estimate < limit then
    redis.call('INCR', curr_key)
    if curr == 0 then
        redis.call('EXPIRE', curr_key, 2)
    end
    return 1
else
    return 0
end
"""


async def check_qps(username: str, role: str = "user", _now: float | None = None) -> bool:
    """QPS 限流（滑动窗口，服务端时间）；Redis 异常 Fail-open 放行

    _now: 仅供离线单测注入确定性时钟（详见 tests/unit/test_sliding_counter.py）；
          生产为 None → 走 Redis 服务端 TIME，摆脱客户端时钟漂移（P1 #14）。
    """
    try:
        limits = _get_limits(role)
        r = await get_redis()
        ok = await r.eval(
            _QPS_COUNTER_LUA, 0,
            limits["qps"], f"qps:counter:{username}:",
            "" if _now is None else str(_now),
        )
        return ok == 1
    except Exception as e:
        # fail-open 必须可告警：Redis 故障窗口内四道闸全开等于 LLM 成本敞口裸奔
        rate_limit_failopen_total.labels(gate="qps").inc()
        logger.error(f"QPS 限流 Redis 异常，放行请求（Fail-open）: {e}")
        return True


# ============================================================
# 并发控制 Lua
# ============================================================
# 租约 TTL（2026-09-12 清欠 P2：原 120 硬编码散落两处；续期循环 60s < TTL，
# 只要续期正常就不过期，长请求由服务层续期兜底）
LEASE_TTL_SECONDS = 120
# 原子"检查+增量"：超限时不修改计数器（P0 #27 拒绝不占位，杜绝调用方二次释放）；
# 通过时无条件 EXPIRE 30s 自愈（P1 #4，杜绝槽位泄漏）。
# 2026-09-12 清欠 P2：now 改用 Redis 服务端 TIME（原 ARGV 传客户端 time.time()，
# 多实例时钟漂移会让在途租约被提前逐出或滞留），与 QPS 滑动窗口同一时钟源
_CONCURRENT_LUA = """
local limit = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local token = ARGV[3]
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local values = redis.call('HGETALL', KEYS[1])
for i = 1, #values, 2 do
    if tonumber(values[i + 1]) <= now then
        redis.call('HDEL', KEYS[1], values[i])
    end
end
if redis.call('HLEN', KEYS[1]) >= limit then
    return nil
end
redis.call('HSET', KEYS[1], token, now + ttl)
redis.call('EXPIRE', KEYS[1], ttl)
return token
"""


async def check_concurrent(username: str, role: str = "user") -> str | None:
    """检查并占用一个并发槽位；返回租约 token，超限返回 None。"""
    try:
        limits = _get_limits(role)
        r = await get_redis()
        token = uuid.uuid4().hex
        leased = await r.eval(
            _CONCURRENT_LUA, 1, f"concurrent:{username}",
            limits["concurrent"], LEASE_TTL_SECONDS, token,
        )
        return token if leased else None
    except Exception as e:
        rate_limit_failopen_total.labels(gate="concurrent").inc()
        logger.error(f"并发限流 Redis 异常，放行请求（Fail-open）: {e}")
        return "__bypass__"


# 释放：原子递减并下限归零。原 GET→DECR 两步在并发释放时会把计数打成负数（P0 #41）；
# 脚本复用 可复用代码/分布式限流器+lua.py 的社区标准实现（v<=1 直接 DEL，杜绝零值残留）
_CONCURRENT_RELEASE_LUA = """
if redis.call('HDEL', KEYS[1], ARGV[1]) == 1 then
    if redis.call('HLEN', KEYS[1]) == 0 then
        redis.call('DEL', KEYS[1])
    end
    return 1
end
return 0
"""


async def release_concurrent(username: str, token: str = ""):
    """仅租约持有者可释放自己的并发槽位；bypass 无槽位可释放。"""
    if not token or token == "__bypass__":
        return
    try:
        r = await get_redis()
        await r.eval(_CONCURRENT_RELEASE_LUA, 1, f"concurrent:{username}", token)
    except Exception as e:
        logger.debug(f"并发槽位释放失败（TTL 自愈）: {e}")


async def renew_concurrent(username: str, token: str, ttl: int = 120) -> bool:
    """续期并发租约；返回 False 表示租约已丢失。"""
    if not token or token == "__bypass__":
        return False
    try:
        r = await get_redis()
        # 2026-09-12 清欠 P2：过期基线改 Redis 服务端 TIME（时钟源与获取租约一致）
        # 2026-09-12 修复（外部复核 P2）：迟到续期不得复活已过期槽位——
        # HEXISTS 之外还须校验原 expire_at 仍大于服务端当前时间
        lua = (
            "local t = redis.call('TIME'); "
            "local now = tonumber(t[1]) + tonumber(t[2]) / 1000000; "
            "local expire_at = redis.call('HGET', KEYS[1], ARGV[1]); "
            "if expire_at and tonumber(expire_at) > now then "
            "redis.call('HSET', KEYS[1], ARGV[1], now + tonumber(ARGV[2])); "
            "redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2])); return 1 end "
            "return 0"
        )
        return await r.eval(
            lua, 1, f"concurrent:{username}", token, ttl,
        ) == 1
    except Exception as e:
        logger.warning(f"并发租约续期失败: {e}")
        return False


# ============================================================
# 日用量（请求数 + Token 数）
# ============================================================
# 原子累加 Lua：同时 INCRBY 两个 Key 并统一设置 TTL（P1 #3/#15 防账目不一致 / Key 永不过期）
_DAILY_UPDATE_LUA = """
local inc_req = tonumber(ARGV[1])
local inc_token = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
redis.call('INCRBY', KEYS[1], inc_req)
redis.call('INCRBY', KEYS[2], inc_token)
redis.call('EXPIRE', KEYS[1], ttl)
redis.call('EXPIRE', KEYS[2], ttl)
return 1
"""

# 原子“检查限额 + 占用一次请求”：并发请求不能同时读到旧计数后各自放行。
_DAILY_RESERVE_LUA = """
local req = tonumber(redis.call('GET', KEYS[1]) or '0')
local tok = tonumber(redis.call('GET', KEYS[2]) or '0')
local limit_req = tonumber(ARGV[1])
local limit_token = tonumber(ARGV[2])
if req >= limit_req then return 1 end
if tok >= limit_token then return 2 end
redis.call('INCRBY', KEYS[1], 1)
redis.call('EXPIRE', KEYS[1], ARGV[3])
return 0
"""


async def get_daily_usage(username: str, date_str: str):
    """读取日用量（date_str 统一用业务日：上海 05:00 切日，见 core.day_window）"""
    try:
        r = await get_redis()
        req_key = f"daily_req:{username}:{date_str}"
        token_key = f"daily_token:{username}:{date_str}"
        req_raw = await r.get(req_key) or "0"
        token_raw = await r.get(token_key) or "0"
        # 防御：Redis 半开状态返回空/非数字时按 0 处理（P3 #26）
        req_count = int(req_raw) if req_raw.isdigit() else 0
        token_sum = int(token_raw) if token_raw.isdigit() else 0
        return {"request_count": req_count, "token_sum": token_sum}
    except Exception:
        # 全 0 用量会让 ensure_chat_allowed 的日配额前置判断直接放行，
        # 这同样是 fail-open，与其余三闸同口径打点（core P2-6）
        rate_limit_failopen_total.labels(gate="daily_read").inc()
        return {"request_count": 0, "token_sum": 0}


async def update_daily_usage(username: str, date_str: str, inc_request=1, inc_token=0):
    """日用量原子累加（Lua 同时 INCRBY 双 Key + 无条件 TTL，P1 #3/#15）"""
    try:
        r = await get_redis()
        req_key = f"daily_req:{username}:{date_str}"
        token_key = f"daily_token:{username}:{date_str}"
        # TTL 对齐到下一次业务日切（上海 05:00，day_window 单一真相源，L3 统一）；
        # +60s 余量防跨天边界 Key 提前过期（P2）——若请求恰在切日前一秒落在旧日期
        # Key 上，ttl 仅约 1s，可能在毫秒级读到计数前被 Redis 清掉，导致当日计数
        # 丢失。留 60s 缓冲让边界请求计数在重置前被读走。
        from ..core.day_window import seconds_until_flip
        ttl = max(1, seconds_until_flip() + 60)
        await r.eval(
            _DAILY_UPDATE_LUA, 2, req_key, token_key,
            inc_request, inc_token, ttl,
        )
    except Exception as e:
        logger.warning(f"日用量累加失败（不影响主流程）: {e}")


async def reserve_daily_request(username: str, date_str: str, role: str = "user"):
    """原子占用一次日请求；返回 0 成功、1 请求超限、2 Token 超限、3 Redis 降级放行。"""
    try:
        limits = _get_limits(role)
        r = await get_redis()
        req_key = f"daily_req:{username}:{date_str}"
        token_key = f"daily_token:{username}:{date_str}"
        # L3 统一：与 update_daily_usage 同一业务日切 TTL（上海 05:00），
        # 两处 TTL 不一致会让预占键比计数键活得久/死得早
        from ..core.day_window import seconds_until_flip
        ttl = max(1, seconds_until_flip() + 60)
        return int(await r.eval(
            _DAILY_RESERVE_LUA, 2, req_key, token_key,
            limits["daily_req"], limits["daily_token"], ttl,
        ))
    except Exception as e:
        rate_limit_failopen_total.labels(gate="daily").inc()
        logger.warning(f"日请求原子预留失败（降级放行）: {e}")
        return 3


async def rollback_daily_request(username: str, date_str: str) -> None:
    """任务创建失败时回滚刚占用的请求计数，且不允许减成负数。"""
    lua = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current > 0 then redis.call('DECR', KEYS[1]) end
return 1
"""
    try:
        r = await get_redis()
        await r.eval(lua, 1, f"daily_req:{username}:{date_str}")
    except Exception as e:
        logger.debug(f"日请求回滚失败（TTL 自愈）: {e}")

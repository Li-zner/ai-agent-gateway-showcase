import os
import asyncio
import redis.asyncio as aioredis
from urllib.parse import urlparse
from .logging import setup_logging

logger = setup_logging()
redis_client = None
# 建池双检锁（2026-10-08 第四轮 D1：锁从 get_redis 挪进 init_redis 本体）：
# 原先只有 get_redis 持锁，直调 init_redis 的 lifespan/脚本路径仍是裸的，
# 并发首调会建出两个连接池，多出来的那个没有引用可关、连接数白白翻倍
_init_lock = asyncio.Lock()

# Redis 连接池上限（默认 20，可从环境变量覆盖；P0 去掉硬编码）
REDIS_MAX_CONNECTIONS = int(os.getenv("REDIS_MAX_CONNECTIONS", "20"))


def _build_redis_url():
    """构建并清理 Redis URL（用 urlparse 标准化，避免空认证段误判，P1）"""
    url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    parsed = urlparse(url)
    # 空认证段（redis://@host 或 redis://:@host）→ 按无认证重建，避免误判
    if parsed.username == "" and parsed.password in ("", None):
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc += f":{parsed.port}"
        return f"{parsed.scheme}://{netloc}{parsed.path or ''}"
    return url


async def init_redis():
    """初始化 Redis 连接池（在 lifespan 中调用，幂等；并发首调只有一个建池）"""
    global redis_client
    if redis_client is not None:
        return redis_client
    async with _init_lock:
        if redis_client is not None:      # 锁内复检：等锁期间别人已建好
            return redis_client
        redis_url = _build_redis_url()
        # 局部建、探活通过后才发布全局（2026-10-07 登记 P2）：原先 from_url 一返回就
        # 赋给 redis_client，ping 还在途中时别的协程已能 get_redis() 拿到一个随后
        # 被 close 的连接池；失败路径也不再需要把全局清回 None
        client = aioredis.from_url(
            redis_url,
            encoding="utf-8",
            decode_responses=True,
            max_connections=REDIS_MAX_CONNECTIONS,
            socket_connect_timeout=3,
            socket_timeout=3,
            retry_on_timeout=True,
            retry_on_error=[ConnectionError, TimeoutError],   # 网络抖动自动重试（P2）
            health_check_interval=30,
            # 注：pool_timeout（池获取连接超时，P0 #19）需 redis-py>=7，requirements 锁定 5.2，
            # 故移除；高并发下获取连接阻塞由 socket_timeout=3 兜底（部分缓解）。
        )
        try:
            await client.ping()
        except Exception as e:
            # 启动探活失败 → 拒绝启动（fail loudly），避免 get_redis() 返回未初始化对象（P0）
            logger.error(f"Redis 连接失败: {e}")
            await client.close()
            raise RuntimeError("Redis 连接失败，请检查 Redis 配置") from e
        redis_client = client
        logger.info(f"Redis 连接池已初始化: {redis_url.split('@')[-1] if '@' in redis_url else redis_url}")
        return redis_client


async def get_redis():
    """获取 Redis 客户端（首次调用时自动初始化，双检锁见 init_redis）"""
    if redis_client is None:
        await init_redis()
    return redis_client


async def close_redis():
    """关闭 Redis 连接池（在 lifespan 中调用）"""
    global redis_client
    if redis_client:
        await redis_client.close()
        redis_client = None
        logger.info("Redis 连接池已关闭")


# ============================================================
# 原子计数 / 集合助手（2026-10-08 第四轮 D1）
#
# 为什么收在一处：仓内有五六个位点写"先 INCR 再 EXPIRE"两条命令，进程在两步之间
# 被杀就留下一个无 TTL 的键。对固定窗口计数器是"永不过期、逐年累积"，对并发槽位
# 计数更狠——槽位永不归零，该用户之后被永久挡在并发上限外。既有同型 Lua 已散落在
# _ledger._ORDER_SEQ_LUA / auth._LOGIN_FAIL_LUA / sms_lua，这里做成共用入口，
# 位点侧只剩一行。
# ============================================================

# INCR 后仅在"没有 TTL"时补挂：固定窗口不被后续流量续期（TTL -1＝无过期时间）
_INCR_ARM_TTL_LUA = """
local n = redis.call('INCR', KEYS[1])
if redis.call('TTL', KEYS[1]) < 0 then
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1]))
end
return n
"""


async def incr_arm_ttl(r, key: str, ttl: int) -> int:
    """原子自增并保证键带 TTL；已有 TTL 时不续期（返回自增后的计数）。

    传 redis 客户端而非内部 get_redis：调用方本来就持有客户端，且既有测试是靠
    打桩调用方模块的 get_redis 来注入假连接的。
    """
    return int(await r.eval(_INCR_ARM_TTL_LUA, 1, key, ttl))


# SADD 与 EXPIRE 同步下发；集合每次加入成员都重挂 TTL（滑动窗口），
# 与计数器的"不续期"口径刻意相反——反向索引不能比最晚加入的成员先过期。
_SADD_TOUCH_TTL_LUA = """
redis.call('SADD', KEYS[1], ARGV[2])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1]))
return 1
"""


async def sadd_touch_ttl(r, key: str, member: str, ttl: int) -> None:
    """原子把成员加入集合并重挂 TTL（禁掉"加进去了但没 TTL"的残留形态）。"""
    await r.eval(_SADD_TOUCH_TTL_LUA, 1, key, ttl, member)
import os
import asyncio
import asyncpg
from contextlib import asynccontextmanager
from ..core.config import DATABASE_URL, DB_ACQUIRE_TIMEOUT, DB_COMMAND_TIMEOUT
from ..core.logging import setup_logging

logger = setup_logging()

_pool = None
# 建池双检锁（2026-10-08 第四轮 D1）：见 init_pool
_pool_lock = asyncio.Lock()


_POOL_CONFIG = {
    # 默认值按“多进程/多实例部署”优先，避免网关、控制台和 worker 各自
    # 建立 20 条常驻连接把 PostgreSQL 连接预算一次性吃光。高负载实例通过
    # DB_POOL_MIN_SIZE/DB_POOL_MAX_SIZE 显式调大。
    "min_size": int(os.getenv("DB_POOL_MIN_SIZE", "2")),
    "max_size": int(os.getenv("DB_POOL_MAX_SIZE", "10")),
}

# 校验：min_size > max_size 时 asyncpg 报错信息不明确，这里直接给出清晰错误（P0）
if _POOL_CONFIG["min_size"] > _POOL_CONFIG["max_size"]:
    raise ValueError(
        f"DB_POOL_MIN_SIZE({_POOL_CONFIG['min_size']}) 不能大于 "
        f"DB_POOL_MAX_SIZE({_POOL_CONFIG['max_size']})，请检查环境变量"
    )


async def init_pool():
    """初始化连接池（在 lifespan 中调用；并发首调只有一个池）

    双检锁（2026-10-08 第四轮 D1）：CDC worker 的 schema 校验循环会独立调本函数
    重试建池，与 lifespan 或第二个 worker 并发时，原先的"看全局再建"会建出两个池
    ——多出的那个没有引用可关，DB 连接预算被白占一倍（本仓的池上限本就按
    多实例共享预算来配，见 _POOL_CONFIG 注释）。口径同 db_readonly.get_readonly_pool。
    """
    global _pool
    if _pool is not None:
        return _pool
    async with _pool_lock:
        if _pool is not None:             # 锁内复检：等锁期间别人已建好
            return _pool
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL 环境变量未配置！请在 .env 或 compose env_file 中设置数据库连接串。")
        # 安全：连接错误时不暴露完整 DATABASE_URL（可能含密码）
        try:
            _pool = await asyncpg.create_pool(
                DATABASE_URL,
                min_size=_POOL_CONFIG["min_size"],
                max_size=_POOL_CONFIG["max_size"],
                timeout=DB_ACQUIRE_TIMEOUT,                   # 获取连接超时，防高并发无限阻塞（P0）
                command_timeout=DB_COMMAND_TIMEOUT,           # 单条 SQL 执行上限，防半死连接永久占用
                max_inactive_connection_lifetime=300,          # 空闲连接 300s 回收，防连接无限占用（P0）
            )
        except Exception as e:
            logger.error(f"数据库连接池初始化失败: {e}")
            raise RuntimeError("数据库连接失败，请检查数据库配置") from e
        logger.info(f"数据库连接池已初始化 (min={_POOL_CONFIG['min_size']}, max={_POOL_CONFIG['max_size']})")
        return _pool


async def get_pool():
    """获取已初始化的连接池（必须先在 lifespan 中调用 init_pool）"""
    if _pool is None:
        raise RuntimeError("数据库连接池未初始化，请在 lifespan 中调用 init_pool()")
    return _pool


async def close_pool():
    """关闭连接池（在 lifespan 中调用）"""
    global _pool
    if _pool:
        try:
            await _pool.close()
        finally:
            _pool = None


@asynccontextmanager
async def get_db_conn():
    """异步上下文管理器：`async with get_db_conn() as conn`，不是 FastAPI 依赖。"""
    pool = await get_pool()
    conn = await pool.acquire(timeout=DB_ACQUIRE_TIMEOUT)
    try:
        yield conn
    finally:
        await pool.release(conn)

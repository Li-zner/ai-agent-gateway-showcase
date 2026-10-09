"""DB Agent 双通道连接池：dbagent_readonly（读）/ dbagent_writer（受审写）。

来源：DBagent 项目 readonly_db.py + write_db.py 合并搬入（2026-10-02）。
DSN 来自网关 config（DBAGENT_READONLY_DSN / DBAGENT_WRITE_DSN），
均 fail-closed，不回退主账号。写池 max=1：写操作串行化。
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("rag_console.dbagent")

_COMMAND_TIMEOUT = 6.0
# 批写专用：服务端逐条 15s 预算需要客户端上限高于它才吃得到（台账 453）
_BATCH_COMMAND_TIMEOUT = 16.0

_ro_pool = None
_wr_pool = None
_lock = asyncio.Lock()


def _json_safe(value: object) -> object:
    """把 Decimal / datetime / UUID 等转成可序列化形态（证据链与前端消费）。"""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


async def _create(dsn: str, max_size: int):
    """建池；口令不落日志。asyncpg 延迟导入，离线环境不拖累导入。"""
    import asyncpg

    return await asyncpg.create_pool(
        dsn, min_size=0, max_size=max_size, timeout=5.0,
        command_timeout=_COMMAND_TIMEOUT,
        max_inactive_connection_lifetime=180)


async def get_readonly_pool():
    """懒加载读池（max=2，与角色 CONNECTION LIMIT 对齐）。"""
    global _ro_pool
    if _ro_pool is not None:
        return _ro_pool
    async with _lock:
        if _ro_pool is None:
            from ...core.config import DBAGENT_READONLY_DSN

            if not DBAGENT_READONLY_DSN:
                raise RuntimeError("DBAGENT_READONLY_DSN 未配置，DB Agent 只读通道不可用")
            try:
                _ro_pool = await _create(DBAGENT_READONLY_DSN, 2)
            except Exception as exc:
                logger.error("DB Agent 只读池初始化失败: %s", type(exc).__name__)
                raise RuntimeError("DB Agent 只读连接失败，请检查 DBAGENT_READONLY_DSN") from exc
    return _ro_pool


async def get_write_pool():
    """懒加载写池（max=1，写串行化）。"""
    global _wr_pool
    if _wr_pool is not None:
        return _wr_pool
    async with _lock:
        if _wr_pool is None:
            from ...core.config import DBAGENT_WRITE_DSN

            if not DBAGENT_WRITE_DSN:
                raise RuntimeError("DBAGENT_WRITE_DSN 未配置，DB Agent 写通道关闭")
            try:
                _wr_pool = await _create(DBAGENT_WRITE_DSN, 1)
            except Exception as exc:
                logger.error("DB Agent 写池初始化失败: %s", type(exc).__name__)
                raise RuntimeError("DB Agent 写连接失败，请检查 DBAGENT_WRITE_DSN") from exc
    return _wr_pool


async def close_pools() -> None:
    """关两个池（进程退出时调用，幂等）。"""
    global _ro_pool, _wr_pool
    for attr in ("_ro_pool", "_wr_pool"):
        pool = globals().get(attr)
        if pool is not None:
            await pool.close()
            globals()[attr] = None


async def fetch_dicts(sql: str, params: tuple | list = ()) -> list[dict]:
    """只读事务里执行受信语句，返回 dict 行（按位置序转换防重名列覆盖）。"""
    pool = await get_readonly_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL TRANSACTION READ ONLY")
            await conn.execute("SET LOCAL statement_timeout = '5s'")
            records = await conn.fetch(sql, *params)
    return [{k: _json_safe(v) for k, v in
             zip(list(r.keys()), r.values())} for r in records]


async def execute_write(sql: str, params: list) -> list[dict]:
    """执行已过写守卫、已获人工批准的参数化写语句，返回 RETURNING 回读行。

    入口重跑一次 guard_write_sql：本函数即使被旁路调用也执行不了越界语句。
    """
    from .sql_guard import guard_write_sql

    guarded = guard_write_sql(sql)
    pool = await get_write_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL statement_timeout = '5s'")
            records = await conn.fetch(guarded, *params)
    return [{k: _json_safe(v) for k, v in
             zip(list(r.keys()), r.values())} for r in records]


async def execute_write_batch(statements: list[tuple[str, list]]) -> list[list[dict]]:
    """单事务执行一批已过守卫、已获批准的参数化写语句（P1-2 原子组）。

    任一条失败整个事务回滚、异常上抛，由账本层收尾；返回每条的 RETURNING 行。
    入口逐条重跑 guard_write_sql（与 execute_write 同款防线）。超时口径：PG 侧
    `statement_timeout` 是**逐条**生效而非整批，5 条封顶的组最坏 5×15s；客户端
    `command_timeout` 抬到 16s 才让单条 15s 预算真的吃得到（10-08 台账 453，
    此前池级 6s 把单条封顶在 6s，"15s 余量"是虚的）。
    """
    from .sql_guard import guard_write_sql

    guarded = [(guard_write_sql(sql), params) for sql, params in statements]
    pool = await get_write_pool()
    out: list[list[dict]] = []
    async with pool.acquire() as conn:
        await conn.set_command_timeout(_BATCH_COMMAND_TIMEOUT)
        try:
            async with conn.transaction():
                await conn.execute("SET LOCAL statement_timeout = '15s'")
                for sql, params in guarded:
                    records = await conn.fetch(sql, *params)
                    out.append([{k: _json_safe(v) for k, v in
                                 zip(list(r.keys()), r.values())} for r in records])
        finally:
            # 连接归还池前复原：别让下一条单语句写继承 16s 的客户端上限
            await conn.set_command_timeout(_COMMAND_TIMEOUT)
    return out


async def execute_writer_raw(sql: str, params: tuple | list = ()) -> list[dict]:
    """writer 连接上执行受信内部语句（变更账本的登记与状态翻转）。

    与 execute_write 的区别：不重跑 guard_write_sql——账本表本身不在写白名单，
    传入语句全部是本模块构造的常量形态且值走参数绑定。
    """
    pool = await get_write_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL statement_timeout = '5s'")
            records = await conn.fetch(sql, *params)
    return [{k: _json_safe(v) for k, v in
             zip(list(r.keys()), r.values())} for r in records]

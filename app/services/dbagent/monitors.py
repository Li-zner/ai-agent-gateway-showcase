"""数据监控的服务层：SQL 校验、执行、阈值判定、原子认领与落库。

阈值判断是确定性逻辑——调度器、API 与 Agent 工具共用本模块；
LLM 只负责起草与解释，不参与「是否越界」的判定。
"""
from __future__ import annotations

import asyncio
import logging
import operator
import re
from decimal import Decimal, InvalidOperation

from ...core.db import get_pool
from ...core.metrics import dbagent_monitor_checks_total, dbagent_monitor_value
from .sql_guard import guard_sql

logger = logging.getLogger("rag_console.dbagent")

MONITOR_OPS = ("gt", "gte", "lt", "lte")
_OPS = {"gt": operator.gt, "gte": operator.ge,
        "lt": operator.lt, "lte": operator.le}

MONITOR_STATUSES = ("ok", "breach", "unknown")
MONITOR_DEFAULT_INTERVAL = 60
MONITOR_MIN_INTERVAL = 30
MONITOR_MAX_INTERVAL = 86400
MONITOR_MAX_PER_TICK = 5
MONITOR_MAX_COUNT = 20

_NAME_MAX_CHARS = 60
_UNIT_MAX_CHARS = 20
_ERROR_MAX_CHARS = 300

_MONITOR_COLS = (
    "id, name, sql, unit, interval_sec, enabled, op, threshold, "
    "last_value, last_status, last_error, last_run_at, last_ok_at, "
    "next_run_at, created_by, created_at, updated_at")
# 认领语句的 RETURNING 需要表别名前缀，避免与 CTE 的列名歧义。
_MONITOR_COLS_ALIASED = ", ".join(
    f"m.{col.strip()}" for col in _MONITOR_COLS.split(","))

_PLACEHOLDER_RE = re.compile(r"\$\d+")


class MonitorError(ValueError):
    """监控配置或执行结果不合规；调用方把原因展示给操作者。"""


def to_number(value: object) -> Decimal | None:
    """把库值/入参转成有限 Decimal；bool、NaN、Infinity 与非数值一律 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, (int, float)):
        number = Decimal(str(value))
    elif isinstance(value, str):
        try:
            number = Decimal(value.strip())
        except InvalidOperation:
            return None
    else:
        return None
    return number if number.is_finite() else None


def evaluate_threshold(value: object, op: str, threshold: object) -> bool:
    """判断 value 是否越过阈值；op 决定方向（gt/gte/lt/lte）。"""
    compare = _OPS.get(str(op))
    if compare is None:
        raise MonitorError(f"不支持的比较符: {op}（可选: {', '.join(MONITOR_OPS)}）")
    current, limit = to_number(value), to_number(threshold)
    if current is None or limit is None:
        raise MonitorError("监控值或阈值不是有效数值")
    return bool(compare(current, limit))


def read_dialect() -> str:
    """本仓快照只带 PostgreSQL 只读腿，方言固定。"""
    return "postgres"


def _clean_text(value: object, label: str, max_chars: int) -> str:
    """校验必填短文本：非空、不超长。"""
    text = str(value or "").strip()
    if not text:
        raise MonitorError(f"{label}不能为空")
    if len(text) > max_chars:
        raise MonitorError(f"{label}不能超过 {max_chars} 字符")
    return text


def validate_monitor(
    *,
    name: object,
    sql: object,
    op: object,
    threshold: object,
    interval_sec: object = MONITOR_DEFAULT_INTERVAL,
    unit: object = None,
    dialect: str | None = None,
) -> dict:
    """规范化并校验一条监控配置；SQL 必须过守卫且不带参数占位。"""
    raw_sql = str(sql or "").strip().rstrip(";").strip()
    if _PLACEHOLDER_RE.search(raw_sql):
        # 监控项没有 params 通道，带占位符的语句执行时必然失败。
        raise MonitorError("监控 SQL 不支持 $N 参数占位，请写成完整查询")
    guarded = guard_sql(raw_sql)
    op_text = str(op or "").strip().lower()
    if op_text not in _OPS:
        raise MonitorError(f"不支持的比较符: {op}（可选: {', '.join(MONITOR_OPS)}）")
    limit = to_number(threshold)
    if limit is None:
        raise MonitorError("阈值必须是有限数值")
    try:
        interval = int(interval_sec)
    except (TypeError, ValueError) as exc:
        raise MonitorError("采集间隔必须是整数秒") from exc
    if not MONITOR_MIN_INTERVAL <= interval <= MONITOR_MAX_INTERVAL:
        raise MonitorError(
            f"采集间隔必须在 {MONITOR_MIN_INTERVAL}–{MONITOR_MAX_INTERVAL} 秒之间")
    unit_text = None if unit in (None, "") else str(unit).strip()
    if unit_text is not None and len(unit_text) > _UNIT_MAX_CHARS:
        raise MonitorError(f"单位不能超过 {_UNIT_MAX_CHARS} 字符")
    return {
        "name": _clean_text(name, "监控名", _NAME_MAX_CHARS),
        "sql": guarded,
        "unit": unit_text or None,
        "interval_sec": interval,
        "op": op_text,
        "threshold": limit,
    }


async def _fetch_rows(sql: str, dialect: str) -> list[dict]:
    """执行已过守卫的监控语句；外层包 LIMIT 2 防整表入内存。"""
    from .pools import fetch_dicts

    return await fetch_dicts(
        f"SELECT * FROM (\n{sql}\n) AS _monitor_probe LIMIT 2")


async def execute_scalar(sql: str, dialect: str | None = None) -> Decimal:
    """执行监控 SQL 并要求结果恰好单列单行的数值。"""
    rows = await _fetch_rows(sql, dialect or read_dialect())
    if len(rows) != 1:
        raise MonitorError(f"监控 SQL 必须返回单行，实际 {len(rows)} 行")
    row = rows[0]
    if len(row) != 1:
        raise MonitorError(f"监控 SQL 必须返回单列，实际 {len(row)} 列")
    number = to_number(next(iter(row.values())))
    if number is None:
        raise MonitorError("监控 SQL 的返回值不是有效数值")
    return number


async def claim_due(limit: int = MONITOR_MAX_PER_TICK) -> list[dict]:
    """原子认领到期监控并推进 next_run_at；双实例靠 SKIP LOCKED 互斥。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "WITH due AS ("
            " SELECT id FROM db_agent_monitors"
            " WHERE enabled AND (next_run_at IS NULL OR next_run_at <= now())"
            " ORDER BY next_run_at NULLS FIRST"
            " LIMIT $1 FOR UPDATE SKIP LOCKED"
            ") UPDATE db_agent_monitors m"
            " SET next_run_at = now() + make_interval(secs => m.interval_sec)"
            " FROM due WHERE m.id = due.id"
            f" RETURNING {_MONITOR_COLS_ALIASED}",
            limit)
    return [dict(row) for row in rows]


async def _persist_success(monitor_id: int, value: Decimal, status: str) -> None:
    """成功采集：更新值、状态与成功时间，清掉上次错误。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE db_agent_monitors SET last_value = $2, last_status = $3, "
            "last_error = NULL, last_run_at = now(), last_ok_at = now(), "
            "updated_at = now() WHERE id = $1",
            monitor_id, value, status)


async def _persist_failure(monitor_id: int, error: str) -> None:
    """失败采集：转 unknown 并留原因，保留 last_value / last_ok_at 供页面标陈旧。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE db_agent_monitors SET last_status = 'unknown', "
            "last_error = $2, last_run_at = now(), updated_at = now() "
            "WHERE id = $1",
            monitor_id, error[:_ERROR_MAX_CHARS])


def _safe_error(exc: Exception) -> str:
    """错误脱敏沿用 Agent 查询通道的口径（类型 + 错误码，不带连接细节）。"""
    from .agent_tools import _safe_db_error

    return _safe_db_error(exc)


async def run_monitor(row: dict, dialect: str | None = None) -> dict:
    """执行一条监控并落库；执行/落库失败只记 unknown，不把异常抛给调度循环。"""
    dialect = dialect or read_dialect()
    try:
        value = await execute_scalar(row["sql"], dialect)
        status = (
            "breach"
            if evaluate_threshold(value, row["op"], row["threshold"])
            else "ok"
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — 单条失败不拖垮整轮调度
        error = _safe_error(exc)
        await _persist_quietly(_persist_failure, row["id"], error)
        result = {"status": "unknown", "value": None, "error": error}
        _record_metrics(row["id"], result)
        return result
    await _persist_quietly(_persist_success, row["id"], value, status)
    result = {"status": status, "value": value, "error": None}
    _record_metrics(row["id"], result)
    return result


def _record_metrics(monitor_id: int, result: dict) -> None:
    """记录采集结果；打点失败只记 debug，不影响监控本身。"""
    try:
        dbagent_monitor_checks_total.labels(result["status"]).inc()
        if result["value"] is not None:
            dbagent_monitor_value.labels(str(monitor_id)).set(
                float(result["value"]))
    except Exception as exc:  # noqa: BLE001 — 指标通道故障不得影响采集
        logger.debug("监控指标打点失败: %s", type(exc).__name__)


async def _persist_quietly(persist, *args) -> None:
    """落库失败只告警：本轮结果照常返回，下一轮继续尝试。"""
    try:
        await persist(*args)
    except Exception as exc:  # noqa: BLE001 — 落库失败不覆盖原始执行结果
        logger.warning("监控结果落库失败: %s: %s",
                       type(exc).__name__, str(exc)[:120])


async def create_monitor(
    *,
    name: object,
    sql: object,
    op: object,
    threshold: object,
    actor: str,
    interval_sec: object = MONITOR_DEFAULT_INTERVAL,
    unit: object = None,
    dialect: str | None = None,
) -> dict:
    """创建停用草稿；上限保护靠创建时计数（并发下可能多出 1 条，非安全边界）。"""
    fields = validate_monitor(
        name=name, sql=sql, op=op, threshold=threshold,
        interval_sec=interval_sec, unit=unit, dialect=dialect)
    pool = await get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT count(*) FROM db_agent_monitors")
        if count >= MONITOR_MAX_COUNT:
            raise MonitorError(f"监控项已达上限 {MONITOR_MAX_COUNT} 条")
        row = await conn.fetchrow(
            "INSERT INTO db_agent_monitors "
            "(name, sql, unit, interval_sec, enabled, op, threshold, created_by) "
            "VALUES ($1, $2, $3, $4, false, $5, $6, $7) "
            f"RETURNING {_MONITOR_COLS}",
            fields["name"], fields["sql"], fields["unit"],
            fields["interval_sec"], fields["op"], fields["threshold"],
            str(actor or ""))
    return dict(row)


async def update_monitor(monitor_id: int, **changes) -> dict | None:
    """按字段增量更新；启用时立即到期，停用时清掉 next_run_at。"""
    columns: dict[str, object] = {}
    if "name" in changes:
        columns["name"] = _clean_text(changes["name"], "监控名", _NAME_MAX_CHARS)
    if "sql" in changes:
        raw_sql = str(changes["sql"] or "").strip().rstrip(";").strip()
        if _PLACEHOLDER_RE.search(raw_sql):
            raise MonitorError("监控 SQL 不支持 $N 参数占位，请写成完整查询")
        columns["sql"] = guard_sql(raw_sql)
    if "op" in changes:
        op_text = str(changes["op"] or "").strip().lower()
        if op_text not in _OPS:
            raise MonitorError(f"不支持的比较符: {changes['op']}")
        columns["op"] = op_text
    if "threshold" in changes:
        limit = to_number(changes["threshold"])
        if limit is None:
            raise MonitorError("阈值必须是有限数值")
        columns["threshold"] = limit
    if "interval_sec" in changes:
        try:
            interval = int(changes["interval_sec"])
        except (TypeError, ValueError) as exc:
            raise MonitorError("采集间隔必须是整数秒") from exc
        if not MONITOR_MIN_INTERVAL <= interval <= MONITOR_MAX_INTERVAL:
            raise MonitorError(
                f"采集间隔必须在 {MONITOR_MIN_INTERVAL}–{MONITOR_MAX_INTERVAL} 秒之间")
        columns["interval_sec"] = interval
    if "unit" in changes:
        # 显式传 null 表示清空单位；字段缺省时不会进入 changes。
        unit_text = None if changes["unit"] in (None, "") else str(changes["unit"]).strip()
        if unit_text is not None and len(unit_text) > _UNIT_MAX_CHARS:
            raise MonitorError(f"单位不能超过 {_UNIT_MAX_CHARS} 字符")
        columns["unit"] = unit_text or None
    enabled = changes.get("enabled")
    if "enabled" in changes:
        columns["enabled"] = bool(enabled)
    if not columns:
        return await get_monitor(monitor_id)
    sets = [f"{col} = ${index + 2}" for index, col in enumerate(columns)]
    if "enabled" in changes and enabled:
        sets.append("next_run_at = now()")
    elif "enabled" in changes:
        sets.append("next_run_at = NULL")
    sets.append("updated_at = now()")
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE db_agent_monitors SET {', '.join(sets)} "
            f"WHERE id = $1 RETURNING {_MONITOR_COLS}",
            monitor_id, *columns.values())
    return dict(row) if row else None


async def list_monitors() -> list[dict]:
    """列出全部监控项（含停用草稿），按创建顺序。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {_MONITOR_COLS} FROM db_agent_monitors ORDER BY id")
    return [dict(row) for row in rows]


async def get_monitor(monitor_id: int) -> dict | None:
    """按 id 取一条监控项；不存在返回 None。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_MONITOR_COLS} FROM db_agent_monitors WHERE id = $1",
            monitor_id)
    return dict(row) if row else None


async def run_monitor_by_id(monitor_id: int) -> dict | None:
    """手动执行一次（停用草稿也允许，便于启用前验证）；结果一并落库。"""
    row = await get_monitor(monitor_id)
    if row is None:
        return None
    result = await run_monitor(row)
    return {**row, **result}


_LOOP_INTERVAL = 5.0  # 认领扫描周期；监控项自身的间隔由 next_run_at 控制


async def run_due_monitors() -> list[dict]:
    """执行一轮：原子认领到期监控，串行跑完（上一轮不叠加）。"""
    rows = await claim_due()
    return [await run_monitor(row) for row in rows]


async def monitor_loop(interval_sec: float = _LOOP_INTERVAL) -> None:
    """常驻调度循环（由 lifespan 启动）；单轮失败只告警，不退出循环。"""
    while True:
        try:
            await run_due_monitors()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 调度循环不因单轮异常退出
            logger.warning("监控调度轮次失败: %s: %s",
                           type(exc).__name__, str(exc)[:120])
        await asyncio.sleep(interval_sec)

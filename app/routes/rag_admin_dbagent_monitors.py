"""中控台 DB Agent 数据监控端点：列表 / 创建 / 修改 / 立即执行。

挂载点与 dbagent_router 相同（rag_console_api.py，前缀 /api/admin/rag）；
读用 dbagent.view，写用 dbagent.monitor（默认不授予子账号）。
创建一律落停用草稿，启用由页面里的人显式操作。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..core.audit import (
    ACT_DBAGENT_MONITOR_CREATE,
    ACT_DBAGENT_MONITOR_RUN,
    ACT_DBAGENT_MONITOR_UPDATE,
    audit,
)
from ..core.capabilities import require_cap
from ..services.dbagent import monitors as monitor_svc

monitors_router = APIRouter(tags=["admin"])

MonitorOp = Literal["gt", "gte", "lt", "lte"]


class MonitorCreateBody(BaseModel):
    """创建草稿：enabled 不在入参里，服务层固定写 false。"""

    name: str
    sql: str
    op: MonitorOp
    threshold: Decimal
    interval_sec: int = monitor_svc.MONITOR_DEFAULT_INTERVAL
    unit: str | None = None


class MonitorUpdateBody(BaseModel):
    """增量修改；unit 显式传 null 表示清空，未传字段不动。"""

    name: str | None = None
    sql: str | None = None
    op: MonitorOp | None = None
    threshold: Decimal | None = None
    interval_sec: int | None = None
    unit: str | None = None
    enabled: bool | None = None


def _serialize(row: dict) -> dict:
    """Decimal -> float、datetime -> ISO，直接可进 JSON。"""

    def _out(value):
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, datetime):
            return value.isoformat()
        return value

    return {key: _out(value) for key, value in row.items()}


def _raise_http(exc: Exception) -> None:
    """把服务层校验/唯一约束错误翻成可读的 HTTP 状态码。"""
    if exc.__class__.__name__ == "UniqueViolationError":
        raise HTTPException(status_code=409, detail="监控名已存在") from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc


@monitors_router.get("/dbagent/monitors")
async def list_dbagent_monitors(
        current_user: dict = Depends(require_cap("dbagent.view"))) -> list[dict]:
    """全部监控项（含停用草稿），最后值/状态/最后一次执行时间一并返回。"""
    return [_serialize(row) for row in await monitor_svc.list_monitors()]


@monitors_router.post("/dbagent/monitors")
async def create_dbagent_monitor(
        body: MonitorCreateBody,
        current_user: dict = Depends(require_cap("dbagent.monitor"))) -> dict:
    """创建停用草稿；SQL 在落库前必须过只读守卫。"""
    actor = current_user.get("username", "")
    try:
        row = await monitor_svc.create_monitor(
            name=body.name, sql=body.sql, op=body.op,
            threshold=body.threshold, actor=actor,
            interval_sec=body.interval_sec, unit=body.unit)
    except Exception as exc:  # noqa: BLE001 — 统一翻 HTTP，未知错误原样抛
        _raise_http(exc)
    await audit(actor, ACT_DBAGENT_MONITOR_CREATE, {
        "monitor_id": row["id"],
        "name": row["name"],
        "sql_summary": str(row["sql"])[:120],
    })
    return _serialize(row)


@monitors_router.patch("/dbagent/monitors/{monitor_id}")
async def update_dbagent_monitor(
        monitor_id: int,
        body: MonitorUpdateBody,
        current_user: dict = Depends(require_cap("dbagent.monitor"))) -> dict:
    """改名 / SQL / 间隔 / 阈值 / 单位 / 启停；启停即改调度是否认领。"""
    changes = body.model_dump(exclude_unset=True)
    for key in ("name", "sql", "op", "threshold", "interval_sec", "enabled"):
        if key in changes and changes[key] is None:
            raise HTTPException(status_code=422, detail=f"{key} 不能为 null")
    try:
        row = await monitor_svc.update_monitor(monitor_id, **changes)
    except Exception as exc:  # noqa: BLE001 — 统一翻 HTTP，未知错误原样抛
        _raise_http(exc)
    if row is None:
        raise HTTPException(status_code=404, detail="监控项不存在")
    actor = current_user.get("username", "")
    await audit(actor, ACT_DBAGENT_MONITOR_UPDATE, {
        "monitor_id": monitor_id,
        "fields": sorted(changes),
        "enabled": row["enabled"],
    })
    return _serialize(row)


@monitors_router.post("/dbagent/monitors/{monitor_id}/run")
async def run_dbagent_monitor(
        monitor_id: int,
        current_user: dict = Depends(require_cap("dbagent.monitor"))) -> dict:
    """立即执行一次并落库（停用草稿也允许，便于启用前验证）。"""
    result = await monitor_svc.run_monitor_by_id(monitor_id)
    if result is None:
        raise HTTPException(status_code=404, detail="监控项不存在")
    actor = current_user.get("username", "")
    await audit(actor, ACT_DBAGENT_MONITOR_RUN, {
        "monitor_id": monitor_id,
        "status": result.get("status"),
    })
    return _serialize(result)

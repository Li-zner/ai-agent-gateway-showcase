"""请求级 RAG trace：收集入口到输出的阶段事实并批量落库。

采集策略：
- 请求开始时创建 request_uid 并写入 contextvar。
- 各阶段只追加轻量 span，不阻塞主流程。
- 请求收尾时一次性写入 request 根记录和全部 span。
- 采集失败只记日志，绝不影响用户回答。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from ..core.logging import get_trace_id, setup_logging

logger = setup_logging()

_CONFIG_VERSION = os.getenv("RAG_CONFIG_VERSION", "unknown")
_INSTANCE_ID = os.getenv("HOSTNAME", "unknown")
_CURRENT: ContextVar["_RequestTrace | None"] = ContextVar(
    "rag_request_trace", default=None)


@dataclass
class _RequestTrace:
    """单次 RAG 请求的进程内采集状态。"""

    request_uid: str
    started_at: datetime
    started_perf: float
    otel_trace_id: str = ""
    conversation_id: str = ""
    username_hash: str = ""
    persona: str = ""
    route: str = ""
    status: str = "running"
    error_code: str = ""
    selected_model: str = ""
    provider: str = ""
    first_token_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    spans: list[dict[str, Any]] = field(default_factory=list)
    # 检索事实共享容器：begin_request 在父任务先建立本对象，子任务经上下文
    # 拷贝拿到的是同一引用，原位修改父任务可见——contextvar 重绑定做不到这点
    # （ReAct 链路把检索放在 create_task 子任务里执行，见 request_retrieval_state）
    retrieval_facts: dict = field(default_factory=dict)
    finished: bool = False


def _username_hash(username: str) -> str:
    """用户标识只存不可逆摘要，供关联和统计使用。"""
    if not username:
        return ""
    return hashlib.sha256(username.encode("utf-8")).hexdigest()[:32]


def begin_request(*, conversation_id: str = "", username: str = "") -> str:
    """创建请求级上下文；重复调用返回当前 request_uid。"""
    current = _CURRENT.get()
    if current is not None:
        return current.request_uid
    trace = _RequestTrace(
        request_uid=str(uuid.uuid4()),
        started_at=datetime.now(timezone.utc),
        started_perf=time.perf_counter(),
        otel_trace_id=get_trace_id() or "",
        conversation_id=(conversation_id or "")[:128],
        username_hash=_username_hash(username),
    )
    _CURRENT.set(trace)
    return trace.request_uid


def get_request_uid() -> str:
    """返回当前请求 ID；没有请求上下文时返回空字符串。"""
    current = _CURRENT.get()
    return current.request_uid if current else ""


def current_persona() -> str:
    """当前请求上下文中的人格标识；检索/答案侧 trace 据此透传来源标记。

    评测流量以 persona='civil_code_eval' 走完整 chat 链路时，retrieval_traces
    若继续写死缺省值就会丢标记，监控端无法把它与线上流量区分（聚合默认排除
    评测流量正依赖此标记）。无请求上下文时返回空串，由调用方决定兜底。
    """
    current = _CURRENT.get()
    return current.persona if current else ""


def request_retrieval_state() -> dict | None:
    """返回本请求的检索事实共享容器；没有请求上下文时返回 None。

    ReAct 链路把检索放在 create_task 子任务里执行，子任务对 contextvar 的
    重新绑定不会传回父任务，答案侧 record_answer 将拿不到检索快照；改挂在
    请求 trace 对象上按引用共享——begin_request 在父任务先建立，子任务的
    上下文拷贝持有同一对象，子任务原位修改父任务可见。
    """
    current = _CURRENT.get()
    return None if current is None else current.retrieval_facts


def set_request_identity(*, persona: str = "", selected_model: str = "",
                         provider: str = "") -> None:
    """补充人格和模型信息；后写入的非空值覆盖旧值。"""
    current = _CURRENT.get()
    if current is None:
        return
    if persona:
        current.persona = persona[:64]
    if selected_model:
        current.selected_model = selected_model[:64]
    if provider:
        current.provider = provider[:32]


def mark_route(route: str) -> None:
    """记录本次请求最终或当前采用的业务路径。"""
    current = _CURRENT.get()
    if current is not None and route:
        current.route = route[:64]


def mark_first_token() -> None:
    """记录首次产生用户可见输出的耗时。"""
    current = _CURRENT.get()
    if current is None or current.first_token_ms is not None:
        return
    elapsed = (time.perf_counter() - current.started_perf) * 1000
    current.first_token_ms = max(0, int(elapsed))


def add_token_usage(input_tokens: int = 0, output_tokens: int = 0) -> None:
    """累加本次请求的模型 token 用量。"""
    current = _CURRENT.get()
    if current is None or current.finished:
        return
    current.input_tokens = int(current.input_tokens or 0) + max(0, int(input_tokens or 0))
    current.output_tokens = int(current.output_tokens or 0) + max(0, int(output_tokens or 0))


def record_span(stage: str, *, status: str = "ok", latency_ms: int = 0,
                round_no: int = 1, result_count: int | None = None,
                error_code: str = "", attributes: dict | None = None) -> None:
    """追加一个阶段 span；request 上下文缺失时静默跳过。"""
    current = _CURRENT.get()
    if current is None or current.finished:
        return
    ended_at = datetime.now(timezone.utc)
    safe_latency = max(0, int(latency_ms or 0))
    started_at = ended_at - timedelta(milliseconds=safe_latency)
    current.spans.append({
        "span_uid": str(uuid.uuid4()),
        "stage": stage[:32],
        "round_no": max(1, int(round_no or 1)),
        "status": status[:16],
        "started_at": started_at,
        "ended_at": ended_at,
        "latency_ms": safe_latency,
        "result_count": result_count,
        "error_code": error_code[:64],
        "attributes": attributes or {},
    })


def finish_request(*, status: str = "success", error_code: str = "") -> None:
    """结束请求并异步落库；重复调用幂等。"""
    current = _CURRENT.get()
    if current is None or current.finished:
        return
    current.finished = True
    current.status = status
    current.error_code = error_code[:64]
    completed_at = datetime.now(timezone.utc)
    total_ms = max(0, int((time.perf_counter() - current.started_perf) * 1000))
    snapshot = {
        "request_uid": current.request_uid,
        "created_at": current.started_at,
        "completed_at": completed_at,
        "otel_trace_id": current.otel_trace_id,
        "conversation_id": current.conversation_id,
        "username_hash": current.username_hash,
        "persona": current.persona,
        "route": current.route,
        "status": current.status,
        "error_code": current.error_code,
        "selected_model": current.selected_model,
        "provider": current.provider,
        "config_version": _CONFIG_VERSION,
        "first_token_ms": current.first_token_ms,
        "total_ms": total_ms,
        "input_tokens": current.input_tokens,
        "output_tokens": current.output_tokens,
        "instance_id": _INSTANCE_ID,
        "spans": list(current.spans),
    }
    try:
        from ..core.concurrency import spawn
        spawn(_persist(snapshot), name="rag-request-trace")
    except Exception as exc:
        logger.warning("RAG 请求 trace 任务创建失败: %s: %s",
                       type(exc).__name__, exc)
    finally:
        _CURRENT.set(None)


async def _persist(snapshot: dict) -> None:
    """单事务写入请求根和阶段 span；失败只记日志。"""
    try:
        from ..core.db import get_pool

        pool = await get_pool()
        # 2026-09-30 校准修复（B1）：5s 硬编码 → DB_ACQUIRE_TIMEOUT（原先该
        # env 对 finalize 热路径不生效）
        from ..core.config import DB_ACQUIRE_TIMEOUT
        async with pool.acquire(timeout=DB_ACQUIRE_TIMEOUT) as conn:
            async with conn.transaction():
                await _insert_request(conn, snapshot)
                for span in snapshot.get("spans") or []:
                    await _insert_span(conn, snapshot, span)
        from ..core.metrics import (
            rag_request_trace_last_success_timestamp_seconds,
            rag_request_trace_written_total,
        )
        rag_request_trace_written_total.inc()
        rag_request_trace_last_success_timestamp_seconds.set_to_current_time()
    except Exception as exc:
        try:
            from ..core.metrics import rag_request_trace_write_failed_total
            rag_request_trace_write_failed_total.inc()
        except Exception:  # noqa: silent-except — 失败计数指标不可用时不影响落库
            pass
        logger.warning("RAG 请求 trace 落库失败: %s: %s",
                       type(exc).__name__, exc)


async def _insert_request(conn, row: dict) -> None:
    """幂等写请求根表，重复收尾以最后一次完成状态为准。"""
    await conn.execute(
        "INSERT INTO rag_request_traces (request_uid, created_at, completed_at, "
        "otel_trace_id, conversation_id, username_hash, persona, route, status, "
        "error_code, selected_model, provider, config_version, first_token_ms, "
        "total_ms, input_tokens, output_tokens, instance_id, attributes) "
        "VALUES ($1::uuid,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,"
        "$19::jsonb) "
        "ON CONFLICT (request_uid) DO UPDATE SET completed_at=EXCLUDED.completed_at, "
        "status=EXCLUDED.status, error_code=EXCLUDED.error_code, "
        "route=EXCLUDED.route, first_token_ms=EXCLUDED.first_token_ms, "
        "total_ms=EXCLUDED.total_ms, updated_at=now()",
        row["request_uid"], row["created_at"], row["completed_at"],
        row["otel_trace_id"], row["conversation_id"], row["username_hash"],
        row["persona"], row["route"], row["status"], row["error_code"],
        row["selected_model"], row["provider"], row["config_version"],
        row["first_token_ms"], row["total_ms"], row["input_tokens"],
        row["output_tokens"], row["instance_id"],
        json.dumps({"source": "gateway"}, ensure_ascii=False),
    )


async def _insert_span(conn, row: dict, span: dict) -> None:
    """幂等写阶段 span，防止同一快照重试造成重复。"""
    await conn.execute(
        "INSERT INTO rag_stage_spans (span_uid, request_uid, stage, round_no, "
        "status, started_at, ended_at, latency_ms, instance_id, config_version, "
        "result_count, error_code, attributes) "
        "VALUES ($1::uuid,$2::uuid,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb) "
        "ON CONFLICT (span_uid) DO NOTHING",
        span["span_uid"], row["request_uid"], span["stage"], span["round_no"],
        span["status"], span["started_at"], span["ended_at"], span["latency_ms"],
        row["instance_id"], row["config_version"], span["result_count"],
        span["error_code"],
        json.dumps(span["attributes"], ensure_ascii=False),
    )

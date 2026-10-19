"""RAG 控制台只读视图：状态、事件统计、请求时间线与运行期配置。

2026-09-16 从 rag_admin.py 拆出，保持单文件 600 行上限。本模块只读库与
Redis，不写任何状态；权限校验仍由 rag_admin 的路由层负责。
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

from fastapi import HTTPException

from ..services.rag_request_content import (
    fetch_request_content,
    request_content_payload,
)


_VERDICT_WINDOW_HOURS = 24
_RECENT_LIMIT = 20
# 运行期实测值的回溯窗口：超过这个天数的 span 不再代表"当前配置"，
# 只用于 _fetch_runtime_config 取最近一条 rerank_top
_RERANK_OBSERVE_WINDOW_DAYS = 30
_REPORTS_DIR = (
    Path(__file__).resolve().parents[1] / "rag_eval"
)

# 评测流量来源标记（与 retrieval_traces 回填口径一致，见
# tests/eval_traces_to_monitor.py）：这类行不是真实用户请求，默认从运营
# 统计里排除，UI「含评测流量」开关置真时再计入。
# 批1 B-P1-3：值改从 core.persona_manager 取——同一人格 id 现在有两处消费者
# （统计排除 + 画像写入口排除），散抄两份会漂移出"统计排掉了、画像还在收"。
from ..core.persona_manager import EVAL_PERSONA_ID as EVAL_PERSONA


def _eval_filter(idx: int) -> tuple[str, list]:
    """生成「默认排除评测人格」的 SQL 片段：$idx 位由调用方传 include_eval，
    下一个占位符是本函数返回的固定人格值。

    IS DISTINCT FROM 而非 <>：persona 为 NULL 的历史行是真实流量，不能被
    三值逻辑误伤。参数化拼接，无注入面。
    """
    return (f"AND (${idx}::bool OR persona IS DISTINCT FROM ${idx + 1})",
            [EVAL_PERSONA])


def _as_json(v, fallback):
    """jsonb 列读回兼容：asyncpg 默认返回 JSON 字符串（未注册 codec），
    已是 list/dict 则原样返回，字符串尝试解析，失败给 fallback。"""
    import json as _json
    if isinstance(v, (list, dict)):
        return v
    if isinstance(v, str):
        try:
            return _json.loads(v)
        except ValueError:
            return fallback
    return fallback


def _load_latest_evaluation() -> dict | None:
    """读取**最新一份检索类**测评报告（含 chat 真跑反向复算的 live-* 报告）。

    2026-09-18 口径修正：此前只认 batch5 holdout 文件名，真跑报告落盘后
    概览卡仍显示旧基线，被老板抓到"指标都不变"。现在按 mtime 取 reports
    目录里最新的带 detail 列表的报告（kind=retrieval，与 rag_admin_eval
    的识别口径一致），基线/真跑谁新显示谁。
    """
    import json as _json
    candidates = []
    # 两个存放点都扫：batch5 基线在 rag_eval 根目录，其余（含 live 真跑）在 reports/ 子目录
    roots = [_REPORTS_DIR, _REPORTS_DIR / "reports"]
    files = [p for d in roots if d.is_dir() for p in d.glob("*.json")]
    for path in sorted(files, key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = _json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("detail"), list):
            candidates.append((path, data))
            break
    if not candidates:
        return None
    path, data = candidates[0]
    return {
        "report": path.name,
        "evaluated": data.get("evaluated", 0),
        "split": data.get("split", "holdout"),
        "errors": data.get("errors", 0),
        "recall_at_5": data.get("recall@5"),
        "precision_at_5": data.get("precision@5"),
        "recall_at_10": data.get("recall@10"),
        "precision_at_10": data.get("precision@10"),
        "mrr": data.get("mrr"),
        "zero_hit_at_10": data.get("zero_hit_at_10") or [],
        "effective_window": data.get("effective_window"),
        "rerank_top": data.get("rerank_top"),
        "latency_ms": data.get("latency_ms") or {},
    }


async def _fetch_runtime_config(conn) -> dict:
    """运行期检索参数。此前只报「控制台容器自己的 env 默认值」，而控制台不挂
    KB_RERANK_TOP，网关真实跑 12 时界面永远显示 15——正是 09-18 MRR 排查里
    被坑过的静默口径。现在以最新网关 trace 的实测值为主，Redis 覆盖与
    控制台默认只作参照。"""
    default_top = int(os.getenv("KB_RERANK_TOP", "15"))
    override = None
    try:
        from ..core.redis import get_redis
        raw = await (await get_redis()).get("rag:runtime:rerank_top")
        if raw is not None:
            override = int(raw)
    except Exception:
        override = None
    # tools.py 在 retrieval span 的 attributes 里写 rerank_top，取最新一条即实测窗口。
    # 两处门禁（2026-09-22 审查 P2）：
    #   - ~ '^[0-9]+$'：裸 ::int 遇到脏值（手工回填、历史版本写的小数或字符串）
    #     会抛 invalid input syntax，一条坏行就能让整个 /status 500；守卫后脏行
    #     自然被跳过，取到的是最近一条**可用**实测值。
    #   - created_at 窗口：无下界时"最近一条合法值"可能是数月前的配置，把它当
    #     当前运行期口径正是本函数要防的静默错数；超窗即回落到 Redis 覆盖/默认值。
    observed = await conn.fetchval(
        "SELECT (attributes->>'rerank_top')::int FROM rag_stage_spans "
        "WHERE attributes ? 'rerank_top' "
        "AND (attributes->>'rerank_top') ~ '^[0-9]+$' "
        "AND created_at > now() - make_interval(days => $1::int) "
        "ORDER BY id DESC LIMIT 1",
        _RERANK_OBSERVE_WINDOW_DAYS)
    effective = (observed if observed is not None
                 else (override if override is not None else default_top))
    return {
        "rerank_top": effective,
        "rerank_top_observed": observed,
        "rerank_top_override": override,
        "rerank_top_default": default_top,
        "rerank_top_overridden": override is not None,
    }


async def effective_config() -> dict:
    """当前生效配置 + 来源链（默认 → env → 运行期覆盖）——口径漂移的在线唯一真相。

    2026-09-30 口径治理：重排窗 15/12、摘要阈值 15/30/20 这类"一个数字两个家"
    的问题以本端点为准，文档只链接不抄数。只读；Redis 不可达时运行期覆盖
    标注 unavailable 并回落 env/默认（不因 Redis 故障 500）。
    """
    import os

    from ..core.config import _settings
    from ..core.db import _POOL_CONFIG

    def _kv(env_name: str, value, default) -> dict:
        source = "env" if os.getenv(env_name) is not None else "default"
        return {"value": value, "source": source, "default": default, "env": env_name}

    items = {
        "KB_RERANK_TOP": _kv("KB_RERANK_TOP", int(os.getenv("KB_RERANK_TOP", "15")), 15),
        "SUMMARY_THRESHOLD": _kv("SUMMARY_THRESHOLD", _settings.summary_threshold, 30),
        "HISTORY_LIMIT": _kv("HISTORY_LIMIT", _settings.history_limit, 30),
        "DB_POOL_MIN_SIZE": _kv("DB_POOL_MIN_SIZE", _POOL_CONFIG["min_size"], 2),
        "DB_POOL_MAX_SIZE": _kv("DB_POOL_MAX_SIZE", _POOL_CONFIG["max_size"], 10),
        "DB_ACQUIRE_TIMEOUT": _kv("DB_ACQUIRE_TIMEOUT", _settings.db_acquire_timeout, 10.0),
        "DB_COMMAND_TIMEOUT": _kv("DB_COMMAND_TIMEOUT", _settings.db_command_timeout, 30.0),
        "USER_CONCURRENT": _kv("USER_CONCURRENT", _settings.user_concurrent, 200),
        "ADMIN_CONCURRENT": _kv("ADMIN_CONCURRENT", _settings.admin_concurrent, 500),
        "INTENT_USE_LLM": _kv("INTENT_USE_LLM", _settings.intent_use_llm, False),
        "ORCH_ENABLE_PHASE2": _kv("ORCH_ENABLE_PHASE2", _settings.orch_enable_phase2, False),
        "EMBEDDING_TIMEOUT": _kv("EMBEDDING_TIMEOUT", _settings.embedding_timeout, 10.0),
    }

    # 重排窗是唯一带 Redis 运行期覆盖通道的键：检索路径真实消费 get_rerank_top()
    try:
        from ..core.redis import get_redis
        from ..services.rag_runtime_config import (RERANK_TOP_KEY, RERANK_TOP_MAX,
                                                   RERANK_TOP_MIN, get_rerank_top)
        raw = await (await get_redis()).get(RERANK_TOP_KEY)
        if raw is not None and RERANK_TOP_MIN <= int(raw) <= RERANK_TOP_MAX:
            items["KB_RERANK_TOP"].update(value=await get_rerank_top(
                items["KB_RERANK_TOP"]["value"]),
                source="runtime(redis)", runtime_value=int(raw))
        else:
            items["KB_RERANK_TOP"]["runtime_value"] = None
    except Exception:
        items["KB_RERANK_TOP"]["runtime_value"] = "unavailable"
    return {"items": items}


async def _fetch_health_meta(conn) -> dict:
    """读取 incident 和动作队列积压，供控制台健康区展示。"""
    row = await conn.fetchrow(
        "SELECT "
        "(SELECT count(*) FROM rag_incidents WHERE status NOT IN "
        "('resolved','closed')) open_incidents, "
        "(SELECT count(*) FROM rag_remediation_actions "
        "WHERE status='waiting_approval') waiting_actions, "
        "(SELECT count(*) FROM rag_remediation_actions "
        "WHERE status='queued') queued_actions, "
        "(SELECT count(*) FROM rag_remediation_actions "
        "WHERE status='executing') executing_actions, "
        "EXTRACT(EPOCH FROM now() - "
        "(SELECT max(created_at) FROM rag_request_traces))::int "
        "trace_lag_seconds")
    return {
        "open_incidents": row["open_incidents"],
        "waiting_actions": row["waiting_actions"],
        "queued_actions": row["queued_actions"],
        "executing_actions": row["executing_actions"],
        "trace_lag_seconds": row["trace_lag_seconds"],
    }


async def _fetch_status(include_eval: bool = False) -> dict:
    """概览聚合。include_eval=False（默认）时五段统计（结论/明细/检索/答案/
    请求）统一排除评测人格；开关置真可把历史评测流量算回来看。
    评测 trace 在 rag_monitor 扫描层被跳过，不会新产出诊断结论，
    排除口径同时兜住了改动前已入库的那批评测 verdicts。"""
    from ..core.db import get_pool
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        by_code = await conn.fetch(
            "SELECT code, severity, count(*) n FROM rag_verdicts "
            "WHERE created_at > now() - ($1 || ' hours')::interval "
            "AND ($2::bool OR persona IS DISTINCT FROM $3) "
            "GROUP BY code, severity ORDER BY n DESC",
            str(_VERDICT_WINDOW_HOURS), include_eval, EVAL_PERSONA)
        recent = await conn.fetch(
            "SELECT id, created_at, source, source_id, persona, query_snippet, "
            "code, name, severity, title, evidence, action, patch FROM rag_verdicts "
            "WHERE created_at > now() - ($1 || ' hours')::interval "
            "AND ($2::bool OR persona IS DISTINCT FROM $3) "
            "ORDER BY id DESC LIMIT $4",
            str(_VERDICT_WINDOW_HOURS), include_eval, EVAL_PERSONA,
            _RECENT_LIMIT)
        retrieval = await conn.fetchrow(
            "SELECT count(*) total, "
            "count(*) FILTER (WHERE returned_count = 0) empty, "
            "count(*) FILTER (WHERE NOT rerank_used) rerank_dropped, "
            "percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) p95 "
            "FROM retrieval_traces WHERE created_at > now() - ($1 || ' hours')::interval "
            "AND ($2::bool OR persona IS DISTINCT FROM $3)",
            str(_VERDICT_WINDOW_HOURS), include_eval, EVAL_PERSONA)
        answers = await conn.fetchrow(
            "SELECT count(*) total, "
            "count(*) FILTER (WHERE retrieval_returned_count = 0) zero_return "
            "FROM answer_traces WHERE created_at > now() - ($1 || ' hours')::interval "
            "AND ($2::bool OR persona IS DISTINCT FROM $3)",
            str(_VERDICT_WINDOW_HOURS), include_eval, EVAL_PERSONA)
        requests = await conn.fetchrow(
            "SELECT count(*) total, "
            "count(*) FILTER (WHERE status = 'failed') failed, "
            "percentile_cont(0.95) WITHIN GROUP (ORDER BY total_ms) p95 "
            "FROM rag_request_traces "
            "WHERE created_at > now() - ($1 || ' hours')::interval "
            "AND ($2::bool OR persona IS DISTINCT FROM $3)",
            str(_VERDICT_WINDOW_HOURS), include_eval, EVAL_PERSONA)
        health_meta = await _fetch_health_meta(conn)
        runtime_config = await _fetch_runtime_config(conn)
    return {
        "window_hours": _VERDICT_WINDOW_HOURS,
        "verdicts_by_code": [
            {"code": r["code"], "severity": r["severity"], "count": r["n"]}
            for r in by_code],
        "recent_verdicts": [
            {"id": r["id"], "at": str(r["created_at"]), "source": r["source"],
             "source_id": r["source_id"], "persona": r["persona"],
             "query": r["query_snippet"],
             "code": r["code"], "name": r["name"], "severity": r["severity"],
             "title": r["title"],
             "evidence": _as_json(r["evidence"], []),
             "action": r["action"], "patch": _as_json(r["patch"], {})}
            for r in recent],
        "retrieval_24h": {
            "total": retrieval["total"], "empty_recall": retrieval["empty"],
            "rerank_dropped": retrieval["rerank_dropped"],
            "p95_latency_ms": round(retrieval["p95"] or 0),
        },
        "answers_24h": {
            "total": answers["total"], "zero_retrieval": answers["zero_return"],
        },
        "requests_24h": {
            "total": requests["total"], "failed": requests["failed"],
            "p95_latency_ms": round(requests["p95"] or 0),
        },
        "evaluation": _load_latest_evaluation(),
        "runtime_config": runtime_config,
        "health_meta": health_meta,
    }


def _request_row(row) -> dict:
    """请求根记录转 API 结构，时间统一转字符串。"""
    return {
        "request_uid": str(row["request_uid"]),
        "created_at": str(row["created_at"]),
        "completed_at": str(row["completed_at"]) if row["completed_at"] else None,
        "otel_trace_id": row["otel_trace_id"],
        "conversation_id": row["conversation_id"],
        "persona": row["persona"],
        "route": row["route"],
        "status": row["status"],
        "error_code": row["error_code"],
        "selected_model": row["selected_model"],
        "provider": row["provider"],
        "config_version": row["config_version"],
        "first_token_ms": row["first_token_ms"],
        "total_ms": row["total_ms"],
        "input_tokens": row["input_tokens"],
        "output_tokens": row["output_tokens"],
        "instance_id": row["instance_id"],
    }


async def _fetch_requests(limit: int, status: str | None,
                          persona: str | None,
                          include_eval: bool = False) -> list:
    """读取请求列表，只返回摘要字段。默认排除评测人格流量（口径同 _fetch_status）。"""
    from ..core.db import get_pool
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        rows = await conn.fetch(
            "SELECT * FROM rag_request_traces "
            "WHERE ($1::text IS NULL OR status = $1) "
            "AND ($2::text IS NULL OR persona = $2) "
            "AND ($3::bool OR persona IS DISTINCT FROM $4) "
            "ORDER BY created_at DESC LIMIT $5",
            status, persona, include_eval, EVAL_PERSONA, limit)
    return [_request_row(row) for row in rows]


async def _fetch_request_detail(request_uid: uuid.UUID) -> dict:
    """读取单个请求及其阶段 span 和内容侧事实。"""
    from ..core.db import get_pool
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM rag_request_traces WHERE request_uid = $1",
            request_uid)
        if row is None:
            raise HTTPException(status_code=404, detail="请求 trace 不存在")
        spans = await conn.fetch(
            "SELECT span_uid, stage, round_no, status, started_at, ended_at, "
            "latency_ms, result_count, error_code, attributes "
            "FROM rag_stage_spans WHERE request_uid = $1 "
            "ORDER BY COALESCE(started_at, created_at), id",
            request_uid)
        answer, retrievals, chunks = await fetch_request_content(
            conn, request_uid)
    result = _request_row(row)
    result["spans"] = [
        {
            "span_uid": str(span["span_uid"]),
            "stage": span["stage"],
            "round_no": span["round_no"],
            "status": span["status"],
            "started_at": str(span["started_at"]) if span["started_at"] else None,
            "ended_at": str(span["ended_at"]) if span["ended_at"] else None,
            "latency_ms": span["latency_ms"],
            "result_count": span["result_count"],
            "error_code": span["error_code"],
            "attributes": _as_json(span["attributes"], {}),
        }
        for span in spans
    ]
    result["content"] = request_content_payload(answer, retrievals, chunks)
    return result

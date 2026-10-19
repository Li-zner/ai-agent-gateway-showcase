"""RAG 在线监测循环（MVP，2026-09-13）—— 启动即监测自己的检索与回答。

架构（与 retrieval_trace/answer_trace 的职责切分一致）：
- 采集层（已存在）：retrieval_traces 存检索客观事实，answer_traces 存输出侧事实
- 本模块：定时消费两表 → 构造 ragclosure 通用 Trace → 规则诊断 → 结论落 rag_verdicts
- rag_monitor_processing 记录每行已处理状态和规则集版本；健康行也会推进，避免饥饿

能判哪些规则（诚实边界）：trace 表只存了事实数字与 chunk_key，未存块文本，所以
文本类规则（RC-2 位置偏差 / RC-3 碎片化 / RC-5 词面失配 / RC-7 分数无区分度）
**不判**——不存文本就不断文本，避免用编造输入喂规则。当前可判 8 条（见 _RULES）：
  检索侧：RC-1 召回全空 / RC-1a 通路整体不可用 / RC-1b 单腿故障 /
          RC-1c 健康腿零命中观察 / RC-6 重排跳过 / RC-8 延迟超预算
  答案侧：RC-4b 无依据作答 / RC-4c 引用越界
注意 RC-1c 刻意不进 ragclosure.rules._RULES（库层 diagnose() 是单条无聚合入口，
2026-09-15 复审认定"单次零命中即 medium 偏噪"）；持续/窗口语义靠本模块的
incident 聚合机制承载，故只在监测子集判定。
人工介入路径：verdict 自带 evidence + action + patch（ragclosure 规则产出），
由告警/平台层取用；本模块只产出结论，不做自动修复（Phase 3 另行立项）。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from ..core.logging import setup_logging

# 「状态瘦身 + 每日巡检报告」已拆至 rag_monitor_report.py（600 行门禁）；再导出以
# 保持内部调用（rag_monitor_loop 里的 ensure_daily_report）与外部导入路径不变。
from .rag_monitor_report import (
    _ensure_daily_report_locked,
    _slim_status,
    ensure_daily_report,
)

logger = setup_logging()

_ENABLED = os.getenv("RAG_MONITOR_ENABLED", "1") == "1"


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    """env 整数读取防呆：非数字回退默认（否则 import 即 ValueError，
    监测模块整个静默关闭），并钳到下限。"""
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return max(minimum, default)


# 防呆：间隔 0 会让循环无 sleep 死循环打库，批 0 永不处理——钳到可用下限
INTERVAL_SEC = _env_int("RAG_MONITOR_INTERVAL_SEC", 300, minimum=60)
BATCH_LIMIT = _env_int("RAG_MONITOR_BATCH", 500, minimum=50)
RETENTION_DAYS = _env_int("RAG_TRACE_RETENTION_DAYS", 7, minimum=1)
VERDICT_RETENTION_DAYS = _env_int("RAG_VERDICT_RETENTION_DAYS", 30, minimum=1)
INCIDENT_RETENTION_DAYS = _env_int("RAG_INCIDENT_RETENTION_DAYS", 180, minimum=1)
ACTION_RETENTION_DAYS = _env_int("RAG_ACTION_RETENTION_DAYS", 365, minimum=1)

# ragclosure 在仓库根（容器内 /app/ragclosure，见 Dockerfile COPY）
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# 本表可判的规则子集 —— 显式挑选而非全量 diagnose()，原因见模块 docstring
from ragclosure.rules import (_rc1a_recall_unavailable, _rc1_retrieval_miss,  # noqa: E402
                              _rc1b_leg_unavailable, _rc1c_single_leg_empty,
                              _rc4b_ungrounded_answer, _rc4c_citation_out_of_range,
                              _rc6_rerank_skipped, _rc8_latency_outlier, Thresholds)
from ragclosure.schema import (Chunk, RecallLeg, Rerank, Timings, Trace)  # noqa: E402
from . import rag_monitor_poison
from .rag_incidents import refresh_incident, transition_incident, upsert_incident

# 规则语义变更后必须递增该版本；旧版本处理行会被重新诊断一次。
# 版本号必须保持可比较的递增格式；当前规则语义变更后需回放历史 trace。
RULESET_VERSION = os.getenv("RAG_MONITOR_RULESET_VERSION", "2026-09-15.2")

_RULES = {
    "retrieval": (_rc1a_recall_unavailable, _rc1_retrieval_miss,
                  _rc1b_leg_unavailable, _rc1c_single_leg_empty,
                  _rc6_rerank_skipped, _rc8_latency_outlier),
    "answer": (_rc4b_ungrounded_answer, _rc4c_citation_out_of_range),
}

_TRACE_TABLES = {
    "retrieval": "retrieval_traces",
    "answer": "answer_traces",
}


async def _fetch_pending(conn, source: str) -> list:
    """取当前规则集尚未处理的行，每轮限量。

    不设时间窗口：窗口会让「monitor 停机超过窗口期」期间的 trace 永远漏诊；
    处理状态表让健康行也推进，规则集版本变化时再按需重放。LIMIT 分批追账。
    """
    table = _TRACE_TABLES.get(source)
    if table is None:
        raise ValueError(f"未知 trace source: {source}")
    # 评测流量（persona=civil_code_eval）不诊断：184 条批量评测的延迟/降级
    # 会淹没事件队列（RC-8 尤其），其质量结论由「测评」分区的报告承载。
    # 读取时过滤而非写处理标记——这些行永远留在"未处理"但也不阻塞追账
    # （游标靠 LEFT JOIN 反选，不存在 per-row cursor 饥饿问题）。
    rows = await conn.fetch(
        f"SELECT t.* FROM {table} t "
        "LEFT JOIN rag_monitor_processing p "
        "ON p.source = $1 AND p.source_id = t.id AND p.ruleset_version >= $2 "
        "WHERE p.source_id IS NULL "
        "AND t.persona IS DISTINCT FROM 'civil_code_eval' "
        "ORDER BY t.id LIMIT $3",
        source, RULESET_VERSION, BATCH_LIMIT)
    return rows


def _as_key_list(v) -> list:
    """jsonb 列读回兼容：asyncpg 默认返回 JSON 字符串（未注册 codec），兼容两种形态。"""
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, list) else []
        except ValueError:
            return []
    return []


def _row_get(row, key: str, default=None):
    """兼容 asyncpg.Record 与测试用 dict 的取值。"""
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def _to_trace_retrieval(r) -> Trace:
    """retrieval_traces 行 → 通用 Trace。returned 只含 id（无文本），供计数类规则。

    2026-09-14 审计 P1：召回侧 trace 原先重建为空 recalls——单腿故障（如
    embedding 服务挂掉导致向量腿静默为空）在线上永远判不出来。现按行内
    trgm/vec 命中数与 vec_failed 标志重建两条腿，RC-1b 得以消费真实故障状态。
    """
    keys = _as_key_list(r["returned_chunk_keys"])
    # rerank_used NULL（未知）必须透传 None：bool(None)=False 会让 RC-6 误报
    rerank = None if r["rerank_used"] is None else Rerank(used=bool(r["rerank_used"]))
    trgm_hits = _row_get(r, "trgm_hits")
    vec_hits = _row_get(r, "vec_hits")
    recalls: list[RecallLeg] = []
    if trgm_hits is not None or vec_hits is not None:
        vec_failed = bool(_row_get(r, "vec_failed", False))
        recalls = [
            RecallLeg(name="trgm", hits=int(trgm_hits or 0),
                      max_score=float(_row_get(r, "max_trgm_sim") or 0.0)),
            RecallLeg(name="vector", hits=int(vec_hits or 0),
                      max_score=float(_row_get(r, "max_vec_sim") or 0.0),
                      failed=vec_failed,
                      error="embedding unavailable" if vec_failed else ""),
        ]
    return Trace(
        trace_id=str(r["id"]),
        query=r["query"] or "",
        recalls=recalls,
        returned=[Chunk(id=k, text="") for k in keys],
        rerank=rerank,
        timings=Timings(total_ms=float(r["latency_ms"] or 0)),
    )


def _to_trace_answer(r) -> Trace:
    """answer_traces 行 → 通用 Trace。returned 由检索侧快照的 keys 重建。"""
    n = r["retrieval_returned_count"] or 0
    keys = _as_key_list(r["retrieval_keys"])
    if n and len(keys) > n:
        keys = keys[:n]
    return Trace(
        trace_id=str(r["id"]),
        query=r["query"] or "",
        answer=r["answer"] or "",
        returned=[Chunk(id=k, text="") for k in keys],
    )


def _ungrounded_citations_verdict(row) -> dict | None:
    """内容级幻觉引用（RC-4d，2026-09-14 审计 P1）。

    fast path 的 find_ungrounded_citations（回答条文号 vs 依据文本的内容级
    比对，比 RC-4c 的编号越界强一档）结果此前只落 answer_traces 列不入研判。
    现作为独立 verdict 消费，幻觉引用率可在控制台/指标中统计。
    """
    cites = _as_key_list(_row_get(row, "ungrounded_citations"))
    if not cites:
        return None
    return {
        "source": "answer", "source_id": row["id"],
        "request_uid": _row_get(row, "request_uid"),
        "persona": row["persona"] or "civil_code",
        "query_snippet": (row["query"] or "")[:250],
        "code": "RC-4d", "name": "content_ungrounded_citation",
        # 与 RC-4 同族（内容级幻觉外流），按四级裁定标准（rules.py）归 critical
        "severity": "critical",
        "title": f"回答引用了依据文本中不存在的条文号（{len(cites)} 条）",
        "evidence": [f"未命中依据的引用: {', '.join(str(c) for c in cites[:5])}"],
        "action": "核对检索依据是否覆盖回答所引条文；引用应由程序按检索结果编号回填，"
                  "而非模型自由输出条文号。",
        "patch": {"prompt.citation_mode": "programmatic"},
    }


def diagnose_row(source: str, row) -> list:
    """单行跑规则子集，返回 verdict dict 列表。纯函数便于单测。"""
    # 缓存命中 / 法条拦截路径没有检索上下文（retrieval_returned_count 为 NULL，
    # 语义是"未检索"而非"检索为空"）——NULL 压成 0 会让 RC-4b 把正常的缓存回答
    # 误判成无依据作答（生产开语义缓存后高危告警会刷屏），必须跳过依据类规则
    if source == "answer" and row["retrieval_returned_count"] is None:
        # 无检索上下文只跳过依赖召回内容的规则；内容级幻觉引用仍可独立研判。
        verdict = _ungrounded_citations_verdict(row)
        return [verdict] if verdict is not None else []
    th = Thresholds()
    trace = _to_trace_retrieval(row) if source == "retrieval" else _to_trace_answer(row)
    out = []
    for rule in _RULES[source]:
        v = rule(trace, th)
        if v is not None:
            out.append({
                "source": source, "source_id": row["id"],
                "request_uid": _row_get(row, "request_uid"),
                "persona": row["persona"] or "civil_code",
                "query_snippet": (row["query"] or "")[:250],
                "code": v.code, "name": v.name, "severity": v.severity,
                "title": v.title, "evidence": v.evidence,
                "action": v.action, "patch": v.patch,
            })
    if source == "answer":
        v = _ungrounded_citations_verdict(row)
        if v is not None:
            out.append(v)
    return out


async def run_cycle() -> int:
    """跑一轮诊断，返回新落库的 verdict 数。任何失败只记日志。

    多实例部署时用事务级 advisory lock 抢占整轮任务：未抢到的实例立即
    跳过。锁随事务结束自动释放，DB 异常导致回滚时下一轮可安全重试。
    """
    from ..core.db import get_pool
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        result = 0
        async with conn.transaction():
            # 上一轮若有未提交就回滚的 poison 登记，其标记并未落库 → 作废
            rag_monitor_poison.begin_cycle()
            got_lock = await conn.fetchval(
                "SELECT pg_try_advisory_xact_lock(hashtext('rag_monitor_cycle'))")
            if not got_lock:
                logger.debug("RAG 监测：另一实例正在执行本轮，跳过")
                return 0
            result = await _run_cycle_locked(conn)
        # 2026-09-15 审查 B-05：打点必须在事务提交之后——提交失败/回滚时本轮
        # 诊断全部丢失，此时推进 Gauge 会让 RagMonitorStale（900s 阈值）
        # 最多被掩盖一个周期
        # 同理结清 poison 计数（台账 483）：标记随本事务提交才为真，回滚则不结清
        rag_monitor_poison.commit_cleared()
        from ..core.metrics import rag_monitor_last_cycle_timestamp_seconds
        rag_monitor_last_cycle_timestamp_seconds.set_to_current_time()
        return result


async def _run_cycle_locked(conn) -> int:
    """已持锁的单轮诊断实现；与 run_cycle 分离以控制函数长度。"""
    total = 0
    for source in ("retrieval", "answer"):
        try:
            rows = await _fetch_pending(conn, source)
        except Exception as e:
            logger.warning(f"RAG 监测 {source} 侧取行失败（下轮重试）: "
                           f"{type(e).__name__}: {e}")
            continue
        for row in rows:
            source_id = int(row["id"])
            # 每行使用 savepoint：单条数据或 SQL 错误只回滚该行，不得让
            # 后续行持续报 InFailedSQLTransaction 而饿死。
            try:
                async with conn.transaction():
                    try:
                        verdicts = diagnose_row(source, row)
                    except Exception as e:
                        logger.warning(
                            f"RAG 监测：行 {row['id']} 诊断异常（标记跳过）: "
                            f"{type(e).__name__}: {e}")
                        verdicts = [{
                            "source": source, "source_id": row["id"],
                            "persona": "civil_code",
                            "query_snippet": (row["query"] or "")[:250],
                            "code": "RC-ERR", "name": "diagnosis_error",
                            "severity": "low",
                            "title": f"诊断引擎异常: {type(e).__name__}",
                            "evidence": [str(e)[:200]],
                            "action": "查监测日志定位该行", "patch": {},
                        }]
                    inserted = await _persist_verdicts(
                        conn, verdicts, inc_metrics=not _is_backfill_row(row))
                    await _delete_stale_verdicts(
                        conn, source, source_id, verdicts)
                    await _record_processed(conn, source, source_id, verdicts)
                    total += inserted
                # 该行本轮成功 → 清零失败计数，偶发抖动不得累积成终态
                rag_monitor_poison.note_success(source, source_id)
            except Exception as e:
                # 行级落库失败：连续 N 次写 poison，避免毒行永久钉住追账队列
                await rag_monitor_poison.note_failure(conn, source, source_id, e)
        if rows:
            logger.info(f"RAG 监测：{source} 侧诊断 {len(rows)} 行完成")
    return total


def _is_backfill_row(row) -> bool:
    """行龄超过两个监测周期的 trace 属于停机追账/首次全量回填（2026-09-14 审计 P1）。

    回填行的 verdict 照常落库（结论完整性不受影响），但**不递增
    rag_verdicts_total**——历史故障按当前时间打点会让 Alertmanager 对
    存量数据形成回填告警风暴（首轮 500 行可瞬间拉爆告警阈值）。
    """
    created = _row_get(row, "created_at")
    if created is None:
        return False
    try:
        import datetime as _dt
        created_at = (
            created if isinstance(created, _dt.datetime)
            else _dt.datetime.fromisoformat(str(created))
        )
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=_dt.timezone.utc)
        age = (_dt.datetime.now(_dt.timezone.utc) - created_at).total_seconds()
        return age > 2 * INTERVAL_SEC + 60
    except Exception:
        return False


async def _record_processed(conn, source: str, source_id: int,
                            verdicts: list) -> None:
    """记录 trace 已被当前规则集处理，健康行也必须推进。"""
    status = "verdict" if verdicts else "healthy"
    await conn.execute(
        "INSERT INTO rag_monitor_processing "
        "(source, source_id, ruleset_version, status, verdict_count, "
        "processed_at, updated_at) "
        "VALUES ($1,$2,$3,$4,$5,now(),now()) "
        "ON CONFLICT (source, source_id) DO UPDATE SET "
        "ruleset_version=EXCLUDED.ruleset_version, "
        "status=EXCLUDED.status, verdict_count=EXCLUDED.verdict_count, "
        "processed_at=EXCLUDED.processed_at, updated_at=EXCLUDED.updated_at "
        "WHERE EXCLUDED.ruleset_version >= rag_monitor_processing.ruleset_version",
        source, source_id, RULESET_VERSION, status, len(verdicts))


async def _persist_verdicts(conn, verdicts: list,
                            inc_metrics: bool = True) -> int:
    """verdict 落库 + 真插入才计指标（conflict 跳过不计，防虚计）。

    inc_metrics=False 用于回填行（2026-09-14 审计 P1）：结论入库但指标不打点，
    防历史故障触发实时告警。
    """
    inserted_count = 0
    for v in verdicts:
        # 标题在入口统一截断（2026-09-19 审查 F-P3-8）：verdict 列 varchar(128)、
        # incident 列 varchar(256)，只截 SQL 参数会让未截断的同一份 title 流到
        # upsert_incident/refresh_incident，超宽即确定性落库失败并毒住监测队列。
        v = {**v, "title": (v.get("title") or "")[:128]}
        row = await conn.fetchrow(
            "INSERT INTO rag_verdicts (source, source_id, request_uid, persona, "
            "query_snippet, code, name, severity, title, evidence, "
            "action, patch) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11,$12::jsonb) "
            "ON CONFLICT (source, source_id, code) DO UPDATE SET "
            "request_uid=EXCLUDED.request_uid, persona=EXCLUDED.persona, "
            "query_snippet=EXCLUDED.query_snippet, name=EXCLUDED.name, "
            "severity=EXCLUDED.severity, title=EXCLUDED.title, "
            "evidence=EXCLUDED.evidence, action=EXCLUDED.action, "
            "patch=EXCLUDED.patch "
            "RETURNING id, incident_id, (xmax = 0) AS is_insert",
            v["source"], v["source_id"], v.get("request_uid"), v["persona"],
            v["query_snippet"], v["code"], v["name"], v["severity"],
            v["title"],
            json.dumps(v["evidence"], ensure_ascii=False),
            v["action"], json.dumps(v["patch"], ensure_ascii=False))
        if row is None:
            continue
        if not row["is_insert"]:
            incident_id = _row_get(row, "incident_id")
            if incident_id is not None:
                await refresh_incident(conn, int(incident_id), v)
            continue
        inserted_count += 1
        incident_id = await upsert_incident(conn, int(row["id"]), v)
        await conn.execute(
            "UPDATE rag_verdicts SET incident_id=$1 WHERE id=$2",
            incident_id, int(row["id"]))
        # 告警出网（#7）：新事件为 high/critical 时推送 webhook（fire-and-forget）
        if str(v.get("severity")) in ("high", "critical"):
            try:
                from ..core.alerting import spawn_alert
                spawn_alert(
                    f"RAG 事件：{v.get('title') or v.get('code')}",
                    f"事件 #{incident_id}｜规则 {v.get('code')}｜严重度 "
                    f"{v.get('severity')}｜{v.get('action') or '暂无建议动作'}")
            except Exception:  # noqa: silent-except — 告警失败不影响监测
                pass
        if not inc_metrics:
            continue
        try:
            from .rag_recommendations import auto_plan_l0_actions
            await auto_plan_l0_actions(conn, incident_id)
        except Exception as exc:
            logger.warning("RAG 自动规划 L0 动作失败 incident=%s: %s: %s",
                           incident_id, type(exc).__name__, exc)
        try:
            from ..core.metrics import rag_verdicts_total
            rag_verdicts_total.labels(code=v["code"], severity=v["severity"]).inc()
        except Exception:  # noqa: silent-except — 指标失败不影响 verdict
            pass  # 指标失败不影响落库
    return inserted_count


async def _delete_stale_verdicts(conn, source: str, source_id: int,
                                 verdicts: list) -> None:
    """删除规则重放后已不再命中的旧结论，并解决因此空的 incident。

    RAG-1（2026-09-20 审查）：解决一律走 transition_incident（状态守卫 +
    active_key 置空 + 取消未完成修复动作）。原裸 UPDATE 只改 status 不清
    active_key（全表 UNIQUE 仍占位），同规则复发时 upsert_incident 把新
    verdict 永久并进已解决事件、其动作被 _claim_next 排除——治理静默。
    """
    codes = sorted({str(v["code"]) for v in verdicts})
    emptied = await conn.fetch(
        """
        WITH stale AS (
            DELETE FROM rag_verdicts
            WHERE source = $1 AND source_id = $2
              AND NOT (code = ANY($3::text[]))
            RETURNING incident_id
        )
        SELECT DISTINCT incident_id FROM stale WHERE incident_id IS NOT NULL
        """,
        source, source_id, codes,
    )
    for row in emptied:
        incident_id = int(row["incident_id"])
        still_open = await conn.fetchval(
            "SELECT 1 FROM rag_verdicts WHERE incident_id = $1 LIMIT 1",
            incident_id)
        if still_open:
            continue
        # 已 resolved/closed 的事件 transition_incident 会按状态守卫返回 None
        await transition_incident(conn, incident_id, status="resolved")


async def _daily_housekeeping(conn) -> int:
    """每日一次的表清理（三张 trace/结论表过期行）。返回清理总行数。

    retrieval_traces 的 purge_expired 此前零调用方（保留策略只写在注释里），
    统一收敛到这里：answer/retrieval 7 天，verdicts 30 天供趋势分析。
    """
    total = 0
    for table, days in (("retrieval_traces", RETENTION_DAYS),
                        ("answer_traces", RETENTION_DAYS),
                        ("rag_verdicts", VERDICT_RETENTION_DAYS),
                        ("rag_remediation_actions", ACTION_RETENTION_DAYS),
                        ("rag_request_traces", RETENTION_DAYS)):
        n = await conn.fetchval(
            f"WITH del AS (DELETE FROM {table} "
            f"WHERE created_at < now() - ($1 || ' days')::interval RETURNING 1) "
            f"SELECT count(*) FROM del",
            str(int(days)))
        total += int(n or 0)
    closed = await conn.fetchval(
        "WITH del AS (DELETE FROM rag_incidents "
        "WHERE status IN ('resolved','closed') "
        "AND COALESCE(resolved_at, closed_at, last_seen_at, created_at) "
        "< now() - ($1 || ' days')::interval RETURNING 1) "
        "SELECT count(*) FROM del",
        str(int(INCIDENT_RETENTION_DAYS)),
    )
    total += int(closed or 0)
    # 处理状态只服务于仍存在的 trace；trace 被清理后再删状态，不能提前删。
    stale_states = await conn.fetchval(
        "WITH del AS ("
        "DELETE FROM rag_monitor_processing p "
        "WHERE (p.source = 'retrieval' AND NOT EXISTS ("
        "  SELECT 1 FROM retrieval_traces t WHERE t.id = p.source_id)) "
        "OR (p.source = 'answer' AND NOT EXISTS ("
        "  SELECT 1 FROM answer_traces t WHERE t.id = p.source_id)) "
        "RETURNING 1) SELECT count(*) FROM del")
    total += int(stale_states or 0)
    return total


_last_cleanup_date: str | None = None


async def maybe_daily_housekeeping() -> int | None:
    """每天首次循环触发清理（内存日期标记，与报告生成解耦——LLM 挂了
    清理也不能停）。返回清理行数；当天已清理返回 None。"""
    global _last_cleanup_date
    import datetime
    today = datetime.date.today().isoformat()
    if _last_cleanup_date == today:
        return None
    from ..core.db import get_pool
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        async with conn.transaction():
            got_lock = await conn.fetchval(
                "SELECT pg_try_advisory_xact_lock(hashtext('rag_daily_housekeeping'))")
            if not got_lock:
                return None
            cleaned = await _daily_housekeeping(conn)
            _last_cleanup_date = today
            return cleaned


async def rag_monitor_loop(interval_sec: int = None) -> None:
    """常驻循环：每 interval 秒跑一轮诊断；每天第一轮附生成 LLM 巡检报告。"""
    interval = interval_sec or INTERVAL_SEC
    logger.info(f"RAG 监测循环已启动（每 {interval}s 诊断一轮，"
                f"规则子集: RC-1/RC-6/RC-8/RC-4b/RC-4c）")
    while True:
        try:
            n = await run_cycle()
            if n:
                logger.info(f"RAG 监测：本轮新产出 {n} 条诊断结论")
        except Exception as e:
            logger.warning(f"RAG 监测循环异常（继续）: {type(e).__name__}: {e}")
        try:
            await ensure_daily_report()
        except Exception as e:
            logger.warning(f"RAG 每日巡检报告失败（明天再试）: {type(e).__name__}: {e}")
        try:
            cleaned = await maybe_daily_housekeeping()
            if cleaned:
                logger.info(f"RAG 监测：过期 trace/verdict 清理 {cleaned} 行")
        except Exception as e:
            logger.warning(f"RAG 监测清理失败（明天再试）: {type(e).__name__}: {e}")
        await asyncio.sleep(interval)

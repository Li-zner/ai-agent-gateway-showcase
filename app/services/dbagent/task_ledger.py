"""写账本：指纹、归属校验、单笔与批组的登记/快照/回滚（批次 F 步骤 2 自 tasks.py 拆出）。

只依赖 task_state 与 changes/pools 等外部模块；gates 会引这里的 _gate_fingerprint，
故本模块位于 gates 之下、state 之上。"""
from __future__ import annotations

import hashlib
import json
from . import pools
import uuid
from ...core.db import get_pool
from .agent import WriteBlocked
from .changes import (
    _PREVIEW_ROWS,
    _ROLLBACK_CAPTURE_LIMIT,
    _build_reverse_ops,
    _renumbered_where,
    summarize_change,
)
from .task_state import (
    _ABANDONED,
    _FORCE_PROPOSE_LEVELS,
    _GATES,
    _append_step,
    logger,
)


def _rows_fingerprint(rows: list[dict]) -> dict:
    """行数 + 逐行规范化 JSON 排序后的哈希（生产评审⑤执行前复检的比对单元）。

    行序在两次查询间不保证稳定，排序后哈希消序；行 dict 两侧同经
    execute_writer_raw 的 _json_safe 归一化，序列化确定可比。
    """
    canonical = sorted(
        json.dumps(r, sort_keys=True, ensure_ascii=False, default=str)
        for r in rows)
    digest = hashlib.sha1("\n".join(canonical).encode("utf-8")).hexdigest()[:16]
    return {"count": len(rows), "hash": digest}


def _gate_fingerprint(preview_rows: list[dict], risk: dict) -> dict | None:
    """门侧指纹（⑤）：审批挂起时对将影响行做快照指纹，供执行前比对。

    预览未打满 _PREVIEW_ROWS → 行数 + 内容哈希全比对；打满（可能是截断集，
    与执行侧全集无可对齐子集）→ 只比精确行数（风险评估的 COUNT 口径）。
    ponytail 天花板：打满时"行数不变、值被改"检测不到——由回滚账本的
    执行时 before 快照与人审兜底。风险评估失败（unknown）不设指纹。
    """
    if str(risk.get("level") or "") == "unknown":
        return None
    if len(preview_rows) >= _PREVIEW_ROWS:
        return {"count": int(risk.get("affected") or 0), "hash": None}
    return {"count": len(preview_rows), "hash": _rows_fingerprint(preview_rows)["hash"]}


def _fingerprint_mismatch(fp: dict | None, kind: str,
                          before_rows: list[dict]) -> str | None:
    """比对执行前快照与审批预览指纹；不一致返回人话原因，一致/不适用返回 None。"""
    if fp is None or kind not in ("update", "delete"):
        return None
    now = _rows_fingerprint(before_rows)
    if fp["hash"] is not None:
        changed = now["hash"] != fp["hash"] or now["count"] != fp["count"]
    else:
        changed = now["count"] != fp["count"]
    if not changed:
        return None
    expected = (f"预览计数 {fp['count']} 行" if fp["hash"] is None
                else f"预览 {fp['count']} 行")
    return f"{expected}，当前 {now['count']} 行"


async def _actor_policy(actor: str) -> tuple[bool, bool]:
    """提交人权限策略：(是否可提议 DELETE, 是否最高管理员)。

    动词闸（生产评审⑥）：无 dbagent.write_delete 能力则 DELETE 在守卫即拒。
    admin 恒可；查无此人（CLI/评测等非 HTTP 调用方）放行 DELETE 且不视为
    admin——owner 校验以其显式传参为准。查询故障 fail-closed。"""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT role, permissions FROM users WHERE username = $1", actor)
    except Exception as exc:  # noqa: BLE001
        logger.warning("提交人权限查询失败 actor=%s: %s", actor, type(exc).__name__)
        return False, False
    if row is None:
        return True, False  # CLI/评测等受信调用路径
    is_admin = row["role"] == "admin"
    allow_delete = is_admin or "dbagent.write_delete" in (row["permissions"] or [])
    return allow_delete, is_admin


def _require_unchanged(task_id: str, kind: str, before_rows: list[dict]) -> None:
    """单条写执行前复检（生产评审⑤）：审批预览指纹与当前数据不一致即拒。

    抛 WriteBlocked 时未写任何数据、账本无登记——模型收到"已被并发修改"
    的观察后应重新核对提案，而不是重试原语句。
    """
    reason = _fingerprint_mismatch(
        _GATES.get(task_id, {}).get("last_fingerprint"), kind, before_rows)
    if reason:
        raise WriteBlocked(
            f"数据在审批等待期间已被并发修改（{reason}），"
            "未执行任何写入。请重新核对数据后再提案。")


async def _ledger_register_proposal(task_id: str, sql: str, params: list,
                                    question: str, actor: str, steps: list,
                                    tag: str) -> None:
    """影子期/高危转人工：把 guarded SQL 登记为 proposed，一条数据都不写。

    2026-10-09 拆自 _wrap_write_with_ledger.write（≤80 行门禁），判据与文案不变。
    """
    from .sql_guard import write_target_table as _wtt
    table = _wtt(sql)
    approved_by = _GATES.get(task_id, {}).get("last_approved_by", "")
    await pools.execute_writer_raw(
        "INSERT INTO db_change_log (task_id, question, action, target_table, "
        "sql_text, params, summary, status, approved_by, actor) "
        "VALUES ($1, $7, 'proposed', $2, $3, $4::jsonb, $5, "
        "'awaiting_manual', $6, $8)",
        (task_id, table, sql,
         json.dumps(params, ensure_ascii=False, default=str),
         f"{tag}（未执行，待人工）：{sql[:120]}", approved_by, question,
         actor))
    _append_step(task_id, steps, {"kind": "log", "line":
                        f"{tag}：已登记改动日志，交人工执行（未写库）"})


async def _ledger_capture_before(sql: str, params: list) -> tuple[str, str, list[dict]]:
    """执行前取 (目标表, 动作类型, before 影像)；INSERT 没有前置影像。"""
    renumbered = _renumbered_where(sql, params)
    if renumbered:
        table, where, wparams = renumbered
        kind = "delete" if sql.lstrip().lower().startswith("delete") else "update"
        # 快照走 writer 连接（人审通道真值合法；P1-4 后 readonly 是列级白名单，
        # SELECT * 会在敏感列上权限拒绝——2026-10-04 评测 21 实测）
        before_rows = await pools.execute_writer_raw(
            f"SELECT * FROM {table} WHERE {where}", tuple(wparams))
        return table, kind, before_rows
    from .sql_guard import write_target_table as _wtt
    return _wtt(sql), "insert", []


async def _ledger_register_applied(task_id: str, sql: str, params: list,
                                   question: str, actor: str, steps: list,
                                   table: str, kind: str, before_rows: list[dict],
                                   rows: list[dict]) -> None:
    """写落库后登记回滚档案：影像截断 → reverse_ops → applied/partial 账本。

    after 影像直接用 RETURNING * 回读行（守卫强制全列）——不再按原 WHERE
    重取：SET 改掉 WHERE 引用列时重取恒空、宽 WHERE 在 writer 5s 超时下
    会把已落库的写打成"执行失败"且账本丢失（2026-10-05 深审 T5 双腿）。
    """
    affected = len(rows)
    capped = affected > _ROLLBACK_CAPTURE_LIMIT
    before_rows = before_rows[:_ROLLBACK_CAPTURE_LIMIT]
    after_rows = rows[:_ROLLBACK_CAPTURE_LIMIT]
    reverse_ops = None
    if not capped:
        try:
            reverse_ops = await _build_reverse_ops(
                table, kind, before_rows, after_rows)
        except Exception:  # noqa: BLE001
            reverse_ops = None
    status = "applied"
    note = ""
    if capped or (kind in ("update", "delete") and not reverse_ops):
        status = "partial"
        note = "（超出捕获能力，仅留快照供人工恢复）"
    summary = await summarize_change(kind, table, before_rows, after_rows)
    await pools.execute_writer_raw(
        "INSERT INTO db_change_log (task_id, question, action, target_table, "
        "sql_text, params, before_rows, after_rows, reverse_ops, affected, "
        # 列清单必须与 VALUES 占位符同数（含 actor）：10-06 加 actor 参数时
        # 漏改本清单致 42601，写已提交却无回滚档案——守卫见
        # tests/unit/test_20261007_round4_fixes.py（P0-1）
        "summary, status, approved_by, actor) "
        # question 走参数绑定而非子查询：writer 角色对 db_agent_tasks 无
        # SELECT 权限，子查询会让每笔获批写的账本登记都 InsufficientPrivilege
        # （写已落库却报"写执行失败"，2026-10-04 双实例验收实测抓到）
        "VALUES ($1, $12, 'apply', $2, $3, $4::jsonb, $5::jsonb, $6::jsonb, "
        "$7::jsonb, $8, $9, $10, $11, $13)",
        (task_id, table, sql,
         json.dumps(params, ensure_ascii=False, default=str),
         json.dumps(before_rows, ensure_ascii=False, default=str),
         json.dumps(after_rows, ensure_ascii=False, default=str),
         json.dumps(reverse_ops, ensure_ascii=False, default=str) if reverse_ops else None,
         affected, summary, status, _GATES.get(task_id, {}).get("last_approved_by", ""),
         question, actor))
    _append_step(task_id, steps, {"kind": "log", "line":
                        f"回滚档案已登记（{summary or f'影响 {affected} 行'}）{note}"})


def _wrap_write_with_ledger(db_write, task_id: str, steps: list, question: str = "",
                            propose_only: bool = False, actor: str = ""):
    """批准后的写执行包一层变更账本：捕获 before/after 快照并登记反向操作。

    影子期（propose_only，#10）：写门批准后不执行，把 guarded SQL + params
    登记改动日志（action='proposed'，status='awaiting_manual'）交人工执行，
    返回空行——agent 层据此换"已交人工执行"观察。
    P1-5：提案风险等级 L2/unknown 时同样强制走 proposed（approve 不再等于
    执行——L2 双人复核的便宜替代，等级由门在批准时暂存 _GATES）。
    2026-10-09：三段职责提到模块级 _ledger_* helper，本函数只做编排（门禁）。
    """
    async def write(sql: str, params: list) -> list[dict]:
        if task_id in _ABANDONED:
            # P2-2 竞态收口（深审 T3）：墙钟终态与审批决定的临界窗口里，门可能
            # 读到已落库的 approved——废弃任务绝不再执行真实写，静默拒绝
            # （僵尸循环下一步即 should_stop 停步，观察文本无人消费）
            logger.info("[gate-trace] 废弃任务拒绝执行写 task=%s", task_id)
            return []
        forced_lvl = _GATES.get(task_id, {}).get("last_risk")
        if propose_only or forced_lvl in _FORCE_PROPOSE_LEVELS:
            await _ledger_register_proposal(
                task_id, sql, params, question, actor, steps,
                ("影子期提案" if propose_only
                 else f"L2/unknown 高危提案（风险分级 {forced_lvl}，P1-5 强制转人工）"))
            return []
        table, kind, before_rows = await _ledger_capture_before(sql, params)
        _require_unchanged(task_id, kind, before_rows)
        rows = await db_write(sql, params)
        await _ledger_register_applied(
            task_id, sql, params, question, actor, steps,
            table, kind, before_rows, rows)
        return rows
    return write


async def _batch_register_proposals(task_id: str, statements: list[dict],
                                    question: str, actor: str, steps: list,
                                    tag: str, group_id: str) -> None:
    """影子期/高危转人工（批组）：整组逐条登记 proposed，共享 group_id，不执行任何写。

    2026-10-09 拆自 _wrap_write_batch_with_ledger.write_batch（≤80 行门禁）。
    """
    from .sql_guard import write_target_table as _wtt
    approved_by = _GATES.get(task_id, {}).get("last_approved_by", "")
    for i, st in enumerate(statements, 1):
        await pools.execute_writer_raw(
            "INSERT INTO db_change_log (task_id, question, action, "
            "target_table, sql_text, params, summary, status, group_id, "
            "approved_by, actor) VALUES ($1, $6, 'proposed', $2, $3, "
            "$4::jsonb, $5, 'awaiting_manual', $7, $8, $9)",
            (task_id, _wtt(st["sql"]), st["sql"],
             json.dumps(st["params"], ensure_ascii=False, default=str),
             f"{tag} {i}/{len(statements)}（未执行，待人工，组 {group_id}）",
             question, group_id, approved_by, actor))
    _append_step(task_id, steps, {"kind": "log", "line":
                        f"{tag} {len(statements)} 条已登记改动日志"
                        f"（组 {group_id}），交人工在事务内执行（未写库）"})


async def _batch_capture_metas(statements: list[dict]) -> list[dict]:
    """批组执行前逐条取 (目标表, WHERE, 动作类型, before 影像)。"""
    from .sql_guard import write_target_table as _wtt
    metas: list[dict] = []
    for st in statements:
        renumbered = _renumbered_where(st["sql"], st["params"])
        if renumbered:
            table, where, wparams = renumbered
            kind = ("delete" if st["sql"].lstrip().lower().startswith("delete")
                    else "update")
            # 同上：快照在 writer 连接上取全行
            before = await pools.execute_writer_raw(
                f"SELECT * FROM {table} WHERE {where}", tuple(wparams))
        else:
            table, where, wparams, kind, before = (
                _wtt(st["sql"]), None, None, "insert", [])
        metas.append({"table": table, "where": where, "wparams": wparams,
                      "kind": kind, "before": before})
    return metas


def _batch_require_unchanged(task_id: str, metas: list[dict]) -> None:
    """执行前复检（生产评审⑤，批组）：任一语句快照与审批预览不一致即整组拒。"""
    fps = _GATES.get(task_id, {}).get("last_fingerprints") or []
    for i, (meta, fp) in enumerate(zip(metas, fps), 1):
        reason = _fingerprint_mismatch(fp, meta["kind"], meta["before"])
        if reason:
            raise WriteBlocked(
                f"第 {i}/{len(metas)} 条：数据在审批等待期间已被并发修改"
                f"（{reason}），整组未执行。请重新核对数据后再提案。")


async def _batch_register_group_applied(task_id: str, statements: list[dict],
                                        metas: list[dict], returned: list[list[dict]],
                                        group_id: str, approved_by: str,
                                        question: str, actor: str,
                                        steps: list) -> list[tuple[str, list[dict]]]:
    """批组提交后逐条登记回滚档案，并追加一条"整组原子提交"步骤。

    after 影像 = RETURNING * 回读行（同单条：不再按原 WHERE 重取）。
    """
    entries: list[tuple[str, list[dict]]] = []
    total_affected = 0
    for st, meta, rows in zip(statements, metas, returned):
        affected = len(rows)
        total_affected += affected
        capped = affected > _ROLLBACK_CAPTURE_LIMIT
        before_rows = meta["before"][:_ROLLBACK_CAPTURE_LIMIT]
        after_rows = rows[:_ROLLBACK_CAPTURE_LIMIT]
        reverse_ops = None
        if not capped:
            try:
                reverse_ops = await _build_reverse_ops(
                    meta["table"], meta["kind"], before_rows, after_rows)
            except Exception:  # noqa: BLE001
                reverse_ops = None
        status, note = "applied", ""
        if capped or (meta["kind"] in ("update", "delete") and not reverse_ops):
            status = "partial"
            note = "（超出捕获能力，仅留快照供人工恢复）"
        summary = await summarize_change(
            meta["kind"], meta["table"], before_rows, after_rows)
        await pools.execute_writer_raw(
            "INSERT INTO db_change_log (task_id, question, action, "
            "target_table, sql_text, params, before_rows, after_rows, "
            # 列/占位符与参数元组严格对齐（P0-1 修法）：args 序为
            # $11=group_id、$12=approved_by、$13=question、$14=actor，
            # 旧写法 question 位误绑 $12（=approved_by）且列清单缺 actor
            "reverse_ops, affected, summary, status, group_id, "
            "approved_by, actor) "
            "VALUES ($1, $13, 'apply', $2, $3, $4::jsonb, $5::jsonb, "
            "$6::jsonb, $7::jsonb, $8, $9, $10, $11, $12, $14)",
            (task_id, meta["table"], st["sql"],
             json.dumps(st["params"], ensure_ascii=False, default=str),
             json.dumps(before_rows, ensure_ascii=False, default=str),
             json.dumps(after_rows, ensure_ascii=False, default=str),
             json.dumps(reverse_ops, ensure_ascii=False, default=str) if reverse_ops else None,
             affected, summary, status, group_id, approved_by, question, actor))
        _append_step(task_id, steps, {"kind": "log", "line":
                            f"回滚档案已登记（组 {group_id}：{summary or f'影响 {affected} 行'}）{note}"})
        entries.append((st["sql"], rows))
    _append_step(task_id, steps, {"kind": "log", "line":
                        f"批提案组 {group_id} 原子提交：{len(statements)} 条语句，"
                        f"共影响 {total_affected} 行"})
    return entries


def _wrap_write_batch_with_ledger(db_write_batch, task_id: str, steps: list,
                                  question: str = "", propose_only: bool = False,
                                  actor: str = ""):
    """批提案组的账本包装（P1-2）：一组语句单事务执行，账本逐条登记、共享 group_id。

    执行序：before 快照逐条（只读池，事务前）→ db_write_batch（单事务，任一失败
    整组回滚）→ after 重取逐条（只读池，提交后）→ reverse_ops 逐条 → 账本组登记。
    after 重取在提交后走只读池即可见（组数据已整体落库），与单条实现同款。
    影子期：整组登记 proposed（同组），交人工在事务内执行。
    2026-10-09：四段职责提到模块级 _batch_* helper，本函数只做编排（门禁）。
    """
    async def write_batch(statements: list[dict]) -> list[tuple[str, list[dict]]]:
        if task_id in _ABANDONED:
            # 同单条 write：废弃任务拒绝执行（T3 竞态收口）
            logger.info("[gate-trace] 废弃任务拒绝执行批写 task=%s", task_id)
            return [(st["sql"], []) for st in statements]
        approved_by = _GATES.get(task_id, {}).get("last_approved_by", "")
        forced_lvl = _GATES.get(task_id, {}).get("last_risk")
        group_id = uuid.uuid4().hex[:12]
        if propose_only or forced_lvl in _FORCE_PROPOSE_LEVELS:
            await _batch_register_proposals(
                task_id, statements, question, actor, steps,
                ("影子期批提案" if propose_only
                 else f"L2/unknown 高危批提案（组风险 {forced_lvl}，P1-5 强制转人工）"),
                group_id)
            return [(st["sql"], []) for st in statements]
        metas = await _batch_capture_metas(statements)
        _batch_require_unchanged(task_id, metas)
        returned = await db_write_batch(
            [(st["sql"], st["params"]) for st in statements])
        return await _batch_register_group_applied(
            task_id, statements, metas, returned, group_id, approved_by,
            question, actor, steps)
    return write_batch

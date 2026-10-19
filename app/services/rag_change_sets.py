"""L2 变更集：knowledge_chunks 补充与失效标记的校验、事务应用与快照回滚。

边界（对应 rag/分层自动修复实施计划 Phase 3）：
- 未经审批（动作 approval）绝不写库：应用只发生在 L2 动作 handler 内，
  而 L2 动作必须先过策略引擎的人工审批。
- 冲突 diff 被拒绝：同 source 同条号（content 前缀 ^第X条，与 kb_expand 同口径）
  且既有 chunk 有效时拒绝；已失效的旧版本不参与冲突——先失效再补充是合法替换流。
  注意 heading 是编/章层级（同章多条共享），不能当条号用。
- 回滚基于应用前快照：新增记 ids，失效记 (chunk_key, valid) 原值。
- 应用是幂等的：租约过期重试时若变更集已 applied，跳过重放写入、照常复测。
- 已知天花板（ponytail）：kb_expand 的条号→chunk_key 是 API 进程内缓存，
  worker 应用变更后该缓存不会跨进程失效——启用 KB_NEIGHBOR_ENABLED 时需重启
  API 进程（该模块 docstring 已有同款说明）；默认关闭时无影响。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

# 与 scripts/build_kb_final.py / seed_project_knowledge.py 的 key 口径对齐：
# md5(content) 前 16 位做确定性主键，是既有去重机制。
_SOURCE_PREFIXES = {"civil_code": "civil_", "project": "project_"}
_MAX_ITEMS = 50
_MAX_CONTENT_CHARS = 20000
_MAX_HEADING_CHARS = 200
_KEY_RE = re.compile(r"^[a-z]+_[0-9a-f]{16}$")
_ART_HEAD_RE = re.compile(r"^(第[一二三四五六七八九十百千零两]+条)")


class ChangeSetError(ValueError):
    """变更集不存在或状态不允许当前操作。"""


class ChangeSetRejectedError(ValueError):
    """变更集校验未通过（结构、重复或冲突），拒绝入队。"""


PENDING_STATUS = "pending_approval"


def is_pending_status(status: str | None) -> bool:
    """变更集状态是否处于待审批（None 视为不存在）。"""
    return status == PENDING_STATUS


def chunk_key_for(source: str, content: str) -> str:
    """按既有导入脚本同款口径推导 chunk_key。

    MD5 在这里只是与存量约 1300 条 civil_code 数据一致的确定性去重键
    （scripts/build_kb_final.py:50 同款），不承担任何完整性/安全语义，
    换算法会导致同内容推不出同 key、去重失效，故保持不动。
    """
    prefix = _SOURCE_PREFIXES.get(source)
    if prefix is None:
        raise ChangeSetRejectedError(
            f"source 只允许 {sorted(_SOURCE_PREFIXES)}，收到: {source}")
    return prefix + hashlib.md5(content.encode()).hexdigest()[:16]


def _article_of(content: str) -> str | None:
    m = _ART_HEAD_RE.match(content or "")
    return m.group(1) if m else None


def validate_payload(change_type: str, payload: dict) -> tuple[dict, dict]:
    """纯结构校验：不碰数据库。返回 (clean_payload, report)；致命问题抛错。"""
    if not isinstance(payload, dict):
        raise ChangeSetRejectedError("payload 必须是对象")
    report: dict[str, Any] = {"change_type": change_type, "warnings": []}

    if change_type == "knowledge_add":
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            raise ChangeSetRejectedError("knowledge_add.items 必须是非空数组")
        if len(items) > _MAX_ITEMS:
            raise ChangeSetRejectedError(
                f"单次变更集最多 {_MAX_ITEMS} 条，收到 {len(items)}")
        clean_items, seen = [], {}
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                raise ChangeSetRejectedError(f"items[{i}] 必须是对象")
            content = str(item.get("content") or "").strip()
            if not content:
                raise ChangeSetRejectedError(f"items[{i}].content 不能为空")
            if len(content) > _MAX_CONTENT_CHARS:
                raise ChangeSetRejectedError(
                    f"items[{i}].content 超过 {_MAX_CONTENT_CHARS} 字符")
            source = str(item.get("source") or "civil_code")
            heading = str(item.get("heading") or "")[:_MAX_HEADING_CHARS]
            key = chunk_key_for(source, content)
            if key in seen:
                raise ChangeSetRejectedError(
                    f"批内重复内容: items[{seen[key]}] 与 items[{i}] 同文")
            seen[key] = i
            clean_items.append({
                "chunk_key": key, "source": source, "heading": heading,
                "content": content, "article": _article_of(content),
            })
        clean = {"items": clean_items}
        report.update({
            "item_count": len(clean_items),
            "sources": sorted({it["source"] for it in clean_items}),
            "articles": sorted({it["article"] for it in clean_items
                                if it["article"]}),
        })
        return clean, report

    if change_type == "knowledge_invalidate":
        keys = payload.get("chunk_keys")
        if not isinstance(keys, list) or not keys:
            raise ChangeSetRejectedError(
                "knowledge_invalidate.chunk_keys 必须是非空数组")
        if len(keys) > _MAX_ITEMS:
            raise ChangeSetRejectedError(
                f"单次变更集最多 {_MAX_ITEMS} 条，收到 {len(keys)}")
        clean_keys, bad = [], []
        for i, key in enumerate(keys):
            key = str(key or "").strip()
            if not _KEY_RE.match(key):
                bad.append(f"items[{i}]")
                continue
            if key not in clean_keys:
                clean_keys.append(key)
        if bad:
            raise ChangeSetRejectedError(f"chunk_key 格式非法: {bad}")
        if len(keys) != len(clean_keys):
            report["warnings"].append("批内重复 chunk_key 已合并")
        clean = {"chunk_keys": clean_keys}
        report.update({"item_count": len(clean_keys)})
        return clean, report

    raise ChangeSetRejectedError(f"未知变更类型: {change_type}")


def _match_article(rows, article: str) -> str | None:
    """候选行里条号与目标完全相等的 chunk_key；无则 None。

    候选行来自 LIKE 前缀查询，正则复核防止「第五百三十三条」误吞
    「第五百三十三条之一」这类更长 token（保守判冲突）。
    """
    for chunk_key, content in rows:
        if _article_of(content or "") == article:
            return chunk_key
    return None


async def _active_embedding_models(conn, sources) -> dict:
    """各 source 当前在用（有向量且有效）的 embedding 模型。

    供两处使用：校验报告告知操作者补向量应对齐哪个模型；
    应用时直接写入新行，避免列默认值（旧本地模型）在补向量后错配
    `embedding_model = $3` 过滤条件、新知识对向量路永久不可见。
    """
    models = {}
    for source in sources:
        models[source] = await conn.fetchval(
            "SELECT embedding_model FROM knowledge_chunks "
            "WHERE source=$1 AND valid AND embedding IS NOT NULL "
            "GROUP BY embedding_model ORDER BY count(*) DESC LIMIT 1", source)
    return models


async def validate_against_db(conn, change_type: str, clean: dict) -> dict:
    """数据库面校验：重复、同条号冲突与影响面；冲突抛 ChangeSetRejectedError。

    冲突只针对**有效**（valid）chunk：已失效的旧版本不挡新增，
    「先失效旧版、再补充新版」是合法替换流。
    """
    report: dict[str, Any] = {"warnings": []}
    if change_type == "knowledge_add":
        keys = [it["chunk_key"] for it in clean["items"]]
        existing = {r["chunk_key"] for r in await conn.fetch(
            "SELECT chunk_key FROM knowledge_chunks WHERE chunk_key = ANY($1)",
            keys)}
        if existing:
            raise ChangeSetRejectedError(
                f"chunk 已存在，疑似重复补充: {sorted(existing)}")
        conflicts = []
        for it in clean["items"]:
            if not it["article"]:
                continue
            rows = await conn.fetch(
                "SELECT chunk_key, content FROM knowledge_chunks "
                "WHERE source=$1 AND valid AND content LIKE $2 || '%'",
                it["source"], it["article"])
            conflict_key = _match_article(
                [(r["chunk_key"], r["content"]) for r in rows], it["article"])
            if conflict_key:
                conflicts.append({
                    "item_chunk_key": it["chunk_key"],
                    "existing_chunk_key": conflict_key,
                    "article": it["article"],
                })
        if conflicts:
            raise ChangeSetRejectedError(
                f"同条号不同内容，冲突需人工处理: {conflicts}")
        report["embedding_backfill_required"] = True
        report["active_embedding_models"] = await _active_embedding_models(
            conn, sorted({it["source"] for it in clean["items"]}))
        return report

    keys = clean["chunk_keys"]
    rows = await conn.fetch(
        "SELECT chunk_key, content, valid FROM knowledge_chunks "
        "WHERE chunk_key = ANY($1)", keys)
    found = {r["chunk_key"] for r in rows}
    missing = sorted(set(keys) - found)
    if missing:
        report["warnings"].append(f"chunk_key 不存在，应用时将跳过: {missing}")
    already_invalid = sorted(r["chunk_key"] for r in rows if not r["valid"])
    if already_invalid:
        report["warnings"].append(
            f"以下 chunk 已是失效状态，应用时无变化: {already_invalid}")
    report["articles"] = sorted({a for a in (
        _article_of(r["content"]) for r in rows) if a})
    return report


async def create_change_set(conn, *, change_type: str, payload: dict,
                            created_by: str) -> dict:
    """校验并落一条 pending_approval 变更集；调用方以事务包裹。"""
    clean, report = validate_payload(change_type, payload)
    report["db"] = await validate_against_db(conn, change_type, clean)
    row = await conn.fetchrow(
        "INSERT INTO rag_change_sets "
        "(change_type, target_table, payload, validation_report, status, "
        " created_by) "
        "VALUES ($1, 'knowledge_chunks', $2::jsonb, $3::jsonb, "
        " $4, $5) RETURNING *",
        change_type, json.dumps(clean, ensure_ascii=False),
        json.dumps(report, ensure_ascii=False),
        PENDING_STATUS, (created_by or "")[:64] or None)
    return _change_set_row(row)


_ACTION_LATERAL = (
    "LEFT JOIN LATERAL ("
    " SELECT action_id, status AS latest_action_status"
    " FROM rag_remediation_actions"
    " WHERE params->>'change_set_id' = cs.change_set_id::text"
    " ORDER BY action_id DESC LIMIT 1"
    ") a ON TRUE"
)


async def get_change_set(conn, change_set_id: int) -> dict | None:
    row = await conn.fetchrow(
        "SELECT cs.*, a.action_id AS latest_action_id, "
        "a.latest_action_status FROM rag_change_sets cs "
        + _ACTION_LATERAL + " WHERE cs.change_set_id=$1", change_set_id)
    return _change_set_row(row) if row else None


async def list_change_sets(conn, *, status: str | None = None,
                           limit: int = 20) -> list[dict]:
    if status:
        rows = await conn.fetch(
            "SELECT cs.*, a.action_id AS latest_action_id, "
            "a.latest_action_status FROM rag_change_sets cs "
            + _ACTION_LATERAL + " WHERE cs.status=$1 "
            "ORDER BY cs.change_set_id DESC LIMIT $2", status, limit)
    else:
        rows = await conn.fetch(
            "SELECT cs.*, a.action_id AS latest_action_id, "
            "a.latest_action_status FROM rag_change_sets cs "
            + _ACTION_LATERAL
            + " ORDER BY cs.change_set_id DESC LIMIT $1", limit)
    return [_change_set_row(r) for r in rows]


def _change_set_row(row) -> dict:
    out = {
        "change_set_id": int(row["change_set_id"]),
        "change_type": row["change_type"],
        "target_table": row["target_table"],
        "payload": _json_value(row["payload"]),
        "validation_report": _json_value(row["validation_report"]),
        "snapshot": _json_value(row["snapshot"]),
        "status": row["status"],
        "version": int(row["version"]),
        "created_by": row["created_by"],
        "applied_at": str(row["applied_at"]) if row["applied_at"] else None,
        "rolled_back_at": (str(row["rolled_back_at"])
                           if row["rolled_back_at"] else None),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }
    # get/list 走 LATERAL join 才有；apply/rollback 路径无此列
    if "latest_action_id" in row.keys():
        out["latest_action_id"] = (
            int(row["latest_action_id"])
            if row["latest_action_id"] is not None else None)
        out["latest_action_status"] = row["latest_action_status"]
    return out


def _json_value(value):
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return {}
    return {}


def _snapshot_count(change_type: str, snapshot: dict) -> int:
    """从快照恢复「本变更集实际生效的行数」，供幂等重放时的汇总。"""
    if change_type == "knowledge_add":
        return len(snapshot.get("added_ids") or [])
    return sum(1 for r in snapshot.get("invalidated") or []
               if r.get("was_valid"))


async def apply_change_set(conn, change_set_id: int) -> dict:
    """审批通过后的事务化应用；必须在对 action 的同一事务或紧邻事务中调用。

    先取快照再写库：新增类记 RETURNING id，失效类记每行 valid 原值。
    幂等：应用事务已提交（如 worker 在门禁复测期间死亡、租约过期重试）时
    状态已是 applied，跳过重放写入，由调用方照常复测——基线与复测都取
    应用后状态，等价于对现状的门禁验证。
    """
    row = await conn.fetchrow(
        "SELECT * FROM rag_change_sets WHERE change_set_id=$1 FOR UPDATE",
        change_set_id)
    if row is None:
        raise ChangeSetError(f"变更集不存在: {change_set_id}")
    if row["status"] == "applied":
        snapshot = _json_value(row["snapshot"])
        return {"snapshot": snapshot, "already_applied": True,
                "applied_count": _snapshot_count(row["change_type"], snapshot)}
    if row["status"] != PENDING_STATUS:
        raise ChangeSetError(
            f"变更集 {change_set_id} 状态为 {row['status']}，不允许应用")
    payload = _json_value(row["payload"])
    change_type = row["change_type"]

    if change_type == "knowledge_add":
        # RAG-4（2026-09-20 审查）：创建期校验与审批执行是两个窗口——审批-执行
        # 之间可插入同条号新 valid 行，apply 原先只靠 ON CONFLICT (chunk_key)
        # 挡重复，同条号双真 valid 会并存进召回（法条引用互相矛盾）。
        # 应用事务内重跑数据库面校验，冲突即抛 ChangeSetRejectedError 拒写。
        await validate_against_db(conn, change_type, payload)
        models = await _active_embedding_models(
            conn, sorted({it["source"] for it in payload["items"]}))
        ids = []
        for item in payload["items"]:
            new_id = await conn.fetchval(
                "INSERT INTO knowledge_chunks "
                "(chunk_key, source, heading, content, source_doc, valid, "
                " embedding_model) "
                "VALUES ($1,$2,$3,$4,$5,TRUE,$6) "
                "ON CONFLICT (chunk_key) DO NOTHING RETURNING id",
                item["chunk_key"], item["source"], item["heading"],
                item["content"], item["source"], models.get(item["source"]))
            if new_id:
                ids.append(int(new_id))
        snapshot = {"added_ids": ids}
        summary = {"applied_count": len(ids),
                   "skipped_existing": len(payload["items"]) - len(ids),
                   "embedding_backfill_required": True,
                   "embedding_models": {k: v for k, v in models.items() if v}}
    else:
        keys = payload["chunk_keys"]
        before = await conn.fetch(
            "SELECT chunk_key, valid FROM knowledge_chunks "
            "WHERE chunk_key = ANY($1) FOR UPDATE", keys)
        snapshot = {"invalidated": [
            {"chunk_key": r["chunk_key"], "was_valid": r["valid"]}
            for r in before]}
        await conn.execute(
            "UPDATE knowledge_chunks SET valid=FALSE "
            "WHERE chunk_key = ANY($1) AND valid", keys)
        summary = {
            "applied_count": sum(1 for r in before if r["valid"]),
            "skipped_missing_or_invalid": len(keys) - len(before)
                                          + sum(1 for r in before
                                                if not r["valid"]),
        }

    await conn.execute(
        "UPDATE rag_change_sets SET status='applied', snapshot=$2::jsonb, "
        "applied_at=now(), version=version+1, updated_at=now() "
        "WHERE change_set_id=$1",
        change_set_id, json.dumps(snapshot, ensure_ascii=False))
    return {"snapshot": snapshot, **summary}


def _restore_plan(snapshot: dict) -> tuple[list[int], dict[str, bool]]:
    """从快照得出回滚计划：待删除的新增 ids 与待恢复的 (key→was_valid)。

    纯函数便于单测；恢复时只处理 was_valid=True 的行，之前就失效的行不动。
    """
    added = [int(i) for i in (snapshot.get("added_ids") or [])]
    restore = {r["chunk_key"]: bool(r.get("was_valid"))
               for r in (snapshot.get("invalidated") or [])}
    return added, restore


async def rollback_change_set(conn, change_set_id: int) -> dict:
    """按快照回滚已应用的变更集：删回新增行 / 恢复 valid 原值。

    幂等（台账 482 跨 conn 收口，2026-10-08 主人选「幂等自愈 + 发起留痕」档）：已是
    `rolled_back` 不再抛错，直接回"已回滚"摘要。回滚器在自己的连接与事务里干活，
    与动作行那条事务不同源，存在"变更集已回滚、动作行没写上"的崩溃窗口——那时
    抛错会让运营第二次点回滚永远卡在 failed，放行才让重复点击变成对账自愈。
    removed/restored 是当时的执行计数，事后无从重算，故回 0 并带
    `already_rolled_back` 标记，不假装这次又删了一遍。
    """
    row = await conn.fetchrow(
        "SELECT * FROM rag_change_sets WHERE change_set_id=$1 FOR UPDATE",
        change_set_id)
    if row is None:
        raise ChangeSetError(f"变更集不存在: {change_set_id}")
    if row["status"] == "rolled_back":
        return {"status": "rolled_back", "already_rolled_back": True,
                "removed_count": 0, "restored_count": 0}
    if row["status"] != "applied":
        raise ChangeSetError(
            f"变更集 {change_set_id} 状态为 {row['status']}，无需或不能回滚")
    added_ids, restore = _restore_plan(_json_value(row["snapshot"]) or {})

    removed = 0
    if added_ids:
        result = await conn.execute(
            "DELETE FROM knowledge_chunks WHERE id = ANY($1::bigint[])",
            added_ids)
        removed = int(str(result).split()[-1])
    restored = 0
    for key, was_valid in restore.items():
        if was_valid:
            restored += await conn.execute(
                "UPDATE knowledge_chunks SET valid=TRUE "
                "WHERE chunk_key=$1 AND NOT valid",
                key) == "UPDATE 1"

    await conn.execute(
        "UPDATE rag_change_sets SET status='rolled_back', "
        "rolled_back_at=now(), version=version+1, updated_at=now() "
        "WHERE change_set_id=$1", change_set_id)
    return {"status": "rolled_back", "removed_count": removed,
            "restored_count": restored}

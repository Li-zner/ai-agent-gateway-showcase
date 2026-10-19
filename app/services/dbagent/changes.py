"""变更账本/风险分级/回滚 —— DB Agent 写副作用管理的单一权威实现。

从 tasks.py 拆出（tasks 逼近 600 行上限）：写前风险评估、before/after 快照、
反向操作生成、HG 编号解析、回滚冲突检查与执行都在这里；侧栏回滚端点与
Agent 的 rollback_change 工具共用同一份逻辑，保证口径只有一份。

冲突检查回答的问题是：原变更之后、回滚之前，这些行又被别人改过吗？
任何漂移都整体拒绝——回滚要么精确落在"我们改完时的状态"，要么不动。
"""
from __future__ import annotations

import json
import re

from . import pools
from ...core.metrics import dbagent_rollbacks_total

# ---- 风险分级阈值（ponytail: 写死，接真实库时按表配置化并加锁时长估算）----
_RISK_L2_ROWS = 100
_RISK_L2_SHARE = 0.30
_RISK_L1_ROWS = 5
_RISK_L1_SHARE = 0.05
_PREVIEW_ROWS = 20

_ROLLBACK_CAPTURE_LIMIT = 500  # 快照捕获上限；超过登记 partial，不可自动回滚
_CRITICAL_SHARE = 0.15  # 重大变更线（业主 2026-10-05）：影响 ≥15% 行需最高管理员批准
_REPORT_CAP = 5                # 冲突清单最多展示条数

_REF_RE = re.compile(r"^(?:HG)?(\d+)$", re.IGNORECASE)
_LATEST_WORDS = ("latest", "最新", "最近一笔", "最近一次")

# JSON 往返会丢类型（datetime/Decimal 变 str），反向语句对这些列用
# "$n::text::类型" 形态：占位符按 text 编码（str 可过），由服务端转回原类型
_TEXT_ROUNDTRIP_CASTS = {
    "timestamp with time zone": "timestamptz",
    "timestamp without time zone": "timestamp",
    "date": "date",
    "numeric": "numeric",
}


class RollbackError(ValueError):
    """不可回滚或冲突；消息直接给审批人/模型看。"""


def ref_of(change_id: int) -> str:
    """账本对外编号：HG1 起、按 change_id 递增。"""
    return f"HG{change_id}"


def parse_ref(text: str) -> int | None:
    """'HG3' / 'hg3' / '3' -> 3；其余 None。"""
    m = _REF_RE.match((text or "").strip())
    return int(m.group(1)) if m else None


def is_latest_word(text: str) -> bool:
    return (text or "").strip().lower() in _LATEST_WORDS


def _parse_jsonb(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


async def fetch_change(change_id: int) -> dict | None:
    """取一条账本记录（jsonb 已解析）；不存在返回 None。"""
    rows = await pools.fetch_dicts(
        "SELECT change_id, task_id, question, action, target_table, sql_text, "
        "status, reverse_ops, before_rows, after_rows, affected, group_id, actor "
        "FROM db_change_log WHERE change_id = $1", (change_id,))
    if not rows:
        return None
    entry = dict(rows[0])
    for key in ("reverse_ops", "before_rows", "after_rows"):
        entry[key] = _parse_jsonb(entry.get(key))
    return entry


async def fetch_group(group_id: str) -> list[dict]:
    """取一个批提案组的全部成员（按 change_id 升序=原执行序）；空组返回 []。"""
    rows = await pools.fetch_dicts(
        "SELECT change_id FROM db_change_log WHERE group_id = $1 "
        "ORDER BY change_id", (group_id,))
    members = []
    for row in rows:
        change = await fetch_change(row["change_id"])
        if change:
            members.append(change)
    return members


async def fetch_latest_applied() -> dict | None:
    """最近一笔 applied 的变更（供 Agent 的 latest 指令）。"""
    rows = await pools.fetch_dicts(
        "SELECT change_id FROM db_change_log "
        "WHERE action='apply' AND status='applied' ORDER BY change_id DESC LIMIT 1")
    return await fetch_change(rows[0]["change_id"]) if rows else None


# ---- schema 内省与 WHERE 重编号（预览/风险/反向生成共用）----

async def _column_type_map(table: str) -> dict[str, str]:
    """表的全列类型（反向操作的 text roundtrip cast 依赖它）。

    走 writer 连接：P1-4 列级 GRANT 后，readonly 的 information_schema.columns
    只显示其可读列——敏感表拿不到全列类型，反向 SQL 缺 cast 导致回滚 DataError
    （2026-10-04 组回滚实测：created_at 的 $n::text::date cast 缺失）。
    """
    rows = await pools.execute_writer_raw(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = $1", (table,))
    return {r["column_name"]: r["data_type"] for r in rows}


async def _pk_columns(table: str) -> list[str]:
    """主键列名（按位置序）。走 pg_constraint 数据字典而非
    information_schema.table_constraints——只读角色对后者可能不可见（实测返回空）。"""
    rows = await pools.fetch_dicts(
        "SELECT a.attname FROM pg_index i "
        "JOIN pg_class c ON c.oid = i.indrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord) ON true "
        "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum "
        "WHERE i.indisprimary AND c.relname = $1 AND n.nspname = current_schema() "
        "ORDER BY k.ord", (table,))
    return [r["attname"] for r in rows]


def _ph(i: int, dtype: str) -> str:
    cast = _TEXT_ROUNDTRIP_CASTS.get(dtype)
    return f"${i}::text::{cast}" if cast else f"${i}"


def _renumbered_where(sql: str, params: list) -> tuple[str, str, list] | None:
    """抽出已守卫写语句的 WHERE 并把占位符重编号为连续 $1..$k（SET 里的 $N 不在
    WHERE 里，编号必须连续 asyncpg 才收）。INSERT 没有可预览的既有行，返回 None。
    返回 (表名, WHERE 片段, 参数)——预览与风险评估共用，属受信内部派生（参数绑定）。
    """
    from .sql_guard import extract_where, write_target_table

    where = extract_where(sql)
    if where is None:
        return None
    table = write_target_table(sql)
    order: list[int] = []
    for m in re.finditer(r"\$(\d+)", where):
        n = int(m.group(1))
        if n not in order:
            order.append(n)
    mapping = {old: i + 1 for i, old in enumerate(order)}
    new_where = re.sub(r"\$(\d+)", lambda m: f"${mapping[int(m.group(1))]}", where)
    preview_params = []
    for old in order:
        if old > len(params):
            raise RuntimeError("参数序号越界，无法生成预览")
        preview_params.append(params[old - 1])
    return table, new_where, preview_params


_SET_SIMPLE_RE = re.compile(r"^\s*([a-z_][a-z0-9_]*)\s*=\s*\$(\d+)\s*$",
                            re.IGNORECASE)


def _projected_after(sql: str, params: list,
                     before_rows: list[dict]) -> list[dict] | None:
    """UPDATE 的 after 投影（Diff 视图数据源）：SET 全部是简单"列 = $N"形态时，
    把参数逐列应用到 before 行推出 after 行，供审批卡渲染 before→after 对比。
    SET 含列表达式（如 balance = balance + $1）时返回 None——不猜执行结果，
    UI 只展示 before。守卫后语句的受信内部派生（不重过守卫）。"""
    if not sql.lstrip().lower().startswith("update") or not before_rows:
        return None
    match = re.search(r"\bset\b(.*?)(?=\bwhere\b|\breturning\b)", sql,
                      re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    # 顶层逗号切分 SET 赋值项（括号深度归零才算分隔符）
    parts: list[str] = []
    depth, start = 0, 0
    segment = match.group(1)
    for i, ch in enumerate(segment):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(segment[start:i])
            start = i + 1
    parts.append(segment[start:])
    sets: dict[str, object] = {}
    for part in parts:
        m = _SET_SIMPLE_RE.match(part)
        if not m or int(m.group(2)) > len(params):
            return None
        sets[m.group(1).lower()] = params[int(m.group(2)) - 1]
    projected = []
    for row in before_rows:
        out = dict(row)
        out.update(sets)
        projected.append(out)
    return projected


async def _renumbered_preview(sql: str, params: list) -> dict:
    """从已过写守卫的语句派生 before 预览（受信内部语句，参数绑定，不经守卫）。

    预览是人审通道（真值合法），走 writer 连接而非 readonly——P1-4 列级 GRANT
    后 readonly 对敏感表是列级白名单，SELECT * 会在 phone 列上权限拒绝
    （2026-10-04 评测 21 实测：demo_users 的写提案预览全体失败）。
    """
    renumbered = _renumbered_where(sql, params)
    if renumbered is None:
        return {"kind": "insert", "preview_sql": None, "rows": [],
                "projected": None,
                "note": "INSERT：将新增以下参数对应的新行"}
    table, new_where, preview_params = renumbered
    preview_sql = f"SELECT * FROM {table} WHERE {new_where} LIMIT {_PREVIEW_ROWS}"
    rows = await pools.execute_writer_raw(preview_sql, tuple(preview_params))
    verb = "delete" if sql.lstrip().lower().startswith("delete") else "update"
    return {"kind": verb, "preview_sql": preview_sql, "rows": rows,
            "projected": (_projected_after(sql, params, rows)
                          if verb == "update" else None),
            "note": f"该命令将影响以下 {len(rows)} 行"}


async def _assess_write_risk(sql: str, params: list) -> dict:
    """写操作风险分级（代码计算，不是模型自评）：实查 WHERE 命中行数 vs 表规模。

    critical（业主 2026-10-05 拍板）：影响行数 ≥ 全表 15% 属重大变更，随
    风险结果标记，审批端要求最高管理员批准。评估失败（unknown）不标——
    该档已被强制转人工（批准也不执行），不再叠加审批人限制。"""
    try:
        renumbered = _renumbered_where(sql, params)
        if renumbered is None:
            return {"level": "L0", "reasons": ["INSERT 新增行，不影响既有数据"],
                    "affected": 0, "table_rows": 0, "share": 0.0, "critical": False}
        table, new_where, preview_params = renumbered
        size_rows = await pools.fetch_dicts(
            "SELECT GREATEST(reltuples::bigint, 0) AS n FROM pg_class "
            "WHERE relname = $1", (table,))
        table_rows = int(size_rows[0]["n"]) if size_rows else 0
        cnt_rows = await pools.fetch_dicts(
            f"SELECT COUNT(*)::bigint AS n FROM {table} WHERE {new_where}",
            tuple(preview_params))
        affected = int(cnt_rows[0]["n"]) if cnt_rows else 0
    except Exception as exc:  # noqa: BLE001 — 评不出来就明说，不假装安全
        return {"level": "unknown",
                "reasons": [f"风险评估未完成（{type(exc).__name__}），请按 SQL 与参数人工判断"],
                "affected": 0, "table_rows": 0, "share": None, "critical": False}
    share = (affected / table_rows) if table_rows > 0 else 0.0
    critical = share >= _CRITICAL_SHARE
    if affected == 0:
        return {"level": "L0", "reasons": ["WHERE 未命中任何行，执行不会产生变化"],
                "affected": 0, "table_rows": table_rows,
                "share": 0.0, "critical": False}
    if affected >= _RISK_L2_ROWS or (table_rows > 20 and affected >= _RISK_L2_SHARE * table_rows):
        level = "L2"
        reasons = [f"影响 {affected} 行，约占全表 {table_rows} 行的 {share:.0%}——"
                   "在百万级库上属于批量重写：长事务锁行、回滚成本高、主从延迟放大"]
    elif affected >= _RISK_L1_ROWS or (table_rows > 20 and affected >= _RISK_L1_SHARE * table_rows):
        level = "L1"
        reasons = [f"影响 {affected} 行，约占全表 {table_rows} 行的 {share:.0%}"]
    else:
        level = "L0"
        reasons = [f"仅影响 {affected} 行，小范围点改"]
    if critical:
        reasons.append(f"重大变更：影响行数占全表 {share:.0%}（≥{_CRITICAL_SHARE:.0%}），"
                       "需最高管理员批准")
    if affected >= _RISK_L2_ROWS:
        reasons.append("单语句批量变更超过 100 行：建议分批执行，或先在副本/演练库验证")
    return {"level": level, "reasons": reasons,
            "affected": affected, "table_rows": table_rows,
            "share": round(share, 4), "critical": critical}


async def _build_reverse_ops(table: str, kind: str,
                             before_rows: list[dict],
                             after_rows: list[dict]) -> list[dict] | None:
    """按快照逐行生成反向操作（全部参数绑定，目标仍是白名单表，可过写守卫）。

    UPDATE 反向：按主键定位，SET 回 before 值；DELETE 反向：INSERT 回 before 行；
    INSERT 反向：按整行匹配 DELETE。无主键或对不齐就返回 None（登记为不可自动回滚）。
    """
    cols = list((after_rows or before_rows)[0].keys())
    if not cols:
        return None
    types = await _column_type_map(table)
    pk = await _pk_columns(table) or cols
    if not set(pk).issubset(set(cols)):
        return None
    ops: list[dict] = []
    if kind == "update":
        set_cols = [c for c in cols if c not in pk]
        if not set_cols:
            return None
        before_by_pk = {tuple(str(b[p]) for p in pk): b for b in before_rows}
        for a in after_rows:
            b = before_by_pk.get(tuple(str(a[p]) for p in pk))
            if b is None:
                return None
            sets = ", ".join(f"{c} = {_ph(i + 1, types.get(c, 'text'))}"
                             for i, c in enumerate(set_cols))
            wheres = " AND ".join(
                f"{p} = {_ph(len(set_cols) + j + 1, types.get(p, 'text'))}"
                for j, p in enumerate(pk))
            params = [b[c] for c in set_cols] + [a[p] for p in pk]
            ops.append({"sql": f"UPDATE {table} SET {sets} WHERE {wheres} RETURNING *",
                        "params": params})
    elif kind == "delete":
        col_list = ", ".join(cols)
        values = ", ".join(_ph(i + 1, types.get(c, "text")) for i, c in enumerate(cols))
        for b in before_rows:
            ops.append({"sql": f"INSERT INTO {table} ({col_list}) VALUES ({values}) RETURNING *",
                        "params": [b[c] for c in cols]})
    else:  # insert
        for a in after_rows:
            # 逐行拼谓词：None 列必须 `IS NULL`——`col = $n` 传 NULL 恒假，
            # 整条 DELETE 删 0 行也不报错（P1-10 静默假回滚的形状）。
            # 等值谓词只对非空列连续编号，占位符不留空洞。
            preds: list[str] = []
            params: list = []
            for c in cols:
                if a[c] is None:
                    preds.append(f"{c} IS NULL")
                    continue
                params.append(a[c])
                preds.append(f"{c} = {_ph(len(params), types.get(c, 'text'))}")
            ops.append({"sql": f"DELETE FROM {table} WHERE {' AND '.join(preds)} RETURNING *",
                        "params": params})
    return ops or None


# ---- 回滚：冲突检查与执行（端点与 Agent 工具共用）----

async def _current_row(table: str, pk: list[str], snap: dict) -> dict | None:
    """当前行全列快照对比（冲突检查）。

    走 writer 连接：SELECT * 需全列权限，readonly 在 P1-4 后是列级白名单。
    """
    where = " AND ".join(f"{p} = ${i + 1}" for i, p in enumerate(pk))
    rows = await pools.execute_writer_raw(
        f"SELECT * FROM {table} WHERE {where}", tuple(snap[p] for p in pk))
    return rows[0] if rows else None


async def _row_drift(table: str, pk: list[str], snap: dict) -> str | None:
    """当前行 vs 账本快照逐列比对；不一致 = 后来被改过 = 回滚会覆盖新数据。"""
    key_desc = ", ".join(f"{p}={snap[p]}" for p in pk)
    cur = await _current_row(table, pk, snap)
    if cur is None:
        return f"{table} 行（{key_desc}）已不存在，快照无处回放"
    diffs = [c for c in snap if str(cur.get(c)) != str(snap.get(c))]
    if diffs:
        col = diffs[0]
        return (f"{table} 行（{key_desc}）的 {col} 列已被改为 "
                f"{cur.get(col)!r}，快照值是 {snap.get(col)!r}——回滚会覆盖后来的修改")
    return None


async def rollback_conflicts(change: dict) -> list[str]:
    """回滚前冲突检查；返回人话冲突清单（空 = 可以回滚）。"""
    ops = change.get("reverse_ops") or []
    if not ops:
        return ["没有登记反向操作，无法自动回滚"]
    head = ops[0]["sql"].lstrip().upper()
    table = change.get("target_table") or ""
    pk = await _pk_columns(table)
    conflicts: list[str] = []

    def _pk_ok(row: dict) -> bool:
        return bool(pk) and set(pk).issubset(row)

    if head.startswith("UPDATE") or head.startswith("DELETE"):
        # UPDATE：恢复的是 after 行，要求当前行仍等于我们的 after 快照；
        # DELETE 的反向是插回 before 行，同样要求当时删掉的行没被别人重建/修改。
        for row in (change.get("after_rows") or []):
            if not _pk_ok(row):
                conflicts.append(f"{table} 快照缺少主键信息，无法安全定位行")
                break
            drift = await _row_drift(table, pk, row)
            if drift:
                conflicts.append(drift)
    elif head.startswith("INSERT"):
        # 反向是插回被删的行：要求这些主键现在没被新数据占用
        for row in (change.get("before_rows") or []):
            if not _pk_ok(row):
                conflicts.append(f"{table} 快照缺少主键信息，无法安全定位行")
                break
            if await _current_row(table, pk, row) is not None:
                key_desc = ", ".join(f"{p}={row[p]}" for p in pk)
                conflicts.append(f"{table} 主键（{key_desc}）已存在新数据，无法插回快照行")
    if len(conflicts) > _REPORT_CAP:
        conflicts = conflicts[:_REPORT_CAP] + [f"…其余 {len(conflicts) - _REPORT_CAP} 处冲突略"]
    return conflicts


async def summarize_change(kind: str, table: str,
                           before: list[dict], after: list[dict]) -> str:
    """一句话人话描述改动了什么（改动日志列表与展开区共用）。

    UPDATE 从快照差分取"列 旧值 → 新值"（最多列 3 个）；删/增按主键指认行。
    生成失败返回空串，前端回落到 SQL 摘要。
    """
    try:
        pk = await _pk_columns(table)
        src = (after or before or [{}])[0]
        ident = ", ".join(f"{p}={src[p]}" for p in pk if p in src) if pk else ""
        if kind == "update" and before and after:
            b, a = before[0], after[0]
            diffs = [f"{c} {b.get(c)} → {a.get(c)}"
                     for c in a if str(b.get(c)) != str(a.get(c))]
            if not diffs:
                return f"修改 {table} {ident}：内容无变化".strip()
            head = "；".join(diffs[:3])
            more = f"（另 {len(diffs) - 3} 列）" if len(diffs) > 3 else ""
            return f"修改 {table} {ident}：{head}{more}".strip()
        if kind == "delete":
            return f"删除 {table} {ident}".strip()
        return f"新增 {table} {ident}".strip()
    except Exception:  # noqa: BLE001 — 描述失败不阻塞记账
        return ""


async def _execute_reverse_ops(all_ops: list[dict],
                               op_owner: list[str]) -> tuple[list[str], bool]:
    """单事务倒序执行全组反向操作，返回 (错误列表, 事务是否已提交)。

    P1-10 的结构性安全网：每条反向操作都带 `RETURNING *`（见 _build_reverse_ops），
    回读 0 行 = 这条根本没改动数据。此时事务已提交其余操作，绝不能翻 rolled_back，
    交回调用方标 rollback_failed 留人工现场（committed=True）；
    抛异常则事务整体回滚（committed=False）。
    """
    try:
        results = await pools.execute_write_batch(
            [(op["sql"], op["params"]) for op in all_ops])
    except Exception as exc:  # noqa: BLE001 — 失败要落账本留人工现场
        return [f"{type(exc).__name__}: {str(exc)[:200]}"], False
    missed = [op_owner[i] for i, rows in enumerate(results) if not rows]
    if missed:
        # 同一变更多条 op 一起 0 行时只列一次 ref（dict.fromkeys 保序去重）
        refs = ", ".join(list(dict.fromkeys(missed))[:_REPORT_CAP])
        return ([f"{len(missed)} 条反向操作影响 0 行（数据未改动）：{refs}"], True)
    return [], True


async def execute_rollback(change_id: int, actor: str,
                           owner_actor: str | None = None,
                           owner_is_admin: bool = True) -> dict:
    """回滚一笔 applied 变更：冲突检查 -> 逐条执行反向操作（重过写守卫）-> 翻状态。

    归属校验（业主 2026-10-05 拍板）：最高管理员可回滚任何变更，其他人只能回滚自己发起的
    （账本 actor 列）；owner_actor=None 表示不校验（CLI/受信路径），存量无主行对非 admin 拒绝。
    属于批提案组（group_id，P1-2）的变更按**整组**回滚：全组倒序冲突检查先行（任一冲突整体
    拒绝），再单事务倒序执行全组反向操作；中途失败标 rollback_failed 保留现场，剩余转人工。
    审计在本函数统一落（2026-10-05 深审 A17：此前 Agent 工具是唯一绕过审计的路径），路由不再重复记。
    """
    from ...core.audit import ACT_DBAGENT_CHANGE_ROLLBACK, audit

    change = await fetch_change(change_id)
    if change is None:
        raise RollbackError("变更记录不存在")
    if change.get("action") != "apply":
        raise RollbackError("只有 apply 类变更可回滚")
    if change.get("status") != "applied":
        raise RollbackError(f"当前状态 {change.get('status')} 不可回滚")
    if (owner_actor is not None and not owner_is_admin
            and (change.get("actor") or "") != owner_actor):
        raise RollbackError(
            f"变更 {ref_of(change_id)} 由 {(change.get('actor') or '未知发起人')} 登记，"
            "非最高管理员只能回滚自己发起的变更")
    group_id = change.get("group_id")
    members = await fetch_group(group_id) if group_id else [change]
    if group_id and (not members or any(m.get("status") != "applied" for m in members)):
        raise RollbackError("批提案组存在非 applied 成员，请核对改动日志后整组处理")
    all_ops: list[dict] = []
    op_owner: list[str] = []   # 与 all_ops 同序：每条反向操作归属的变更 ref（0 行校验指认用）
    conflicts: list[str] = []
    for member in reversed(members):   # 倒序 = 与执行相反的顺序回滚
        ops = member.get("reverse_ops") or []
        if not ops:
            raise RollbackError(f"{ref_of(member['change_id'])} 没有可自动执行的反向操作")
        member_conflicts = await rollback_conflicts(member)
        conflicts.extend(f"{ref_of(member['change_id'])}: {c}" for c in member_conflicts)
        all_ops.extend(ops)
        op_owner.extend([ref_of(member["change_id"])] * len(ops))
    if conflicts:
        raise RollbackError("回滚被冲突检查阻止，未执行任何操作：\n- " + "\n- ".join(conflicts[:_REPORT_CAP + 1]))
    errors, committed = await _execute_reverse_ops(all_ops, op_owner)
    ids = [m["change_id"] for m in members]
    # 终翻一律带 status='applied' 前置（10-08 台账 448）：并发双回滚时后者那趟影响 0 行，此前会把
    # 已 rolled_back 的行覆盖成 rollback_failed——数据其实已翻好，账本却留下假失败与多记的指标审计。
    claimed = len(ids)
    if errors:
        flipped = await pools.execute_writer_raw(
            "UPDATE db_change_log SET status='rollback_failed', rollback_error=$1, "
            "rolled_back_by=$2, rolled_back_at=now() "
            "WHERE change_id = ANY($3) AND status='applied' RETURNING change_id",
            ("; ".join(errors)[:500], actor, ids))
        if len(flipped) < claimed:
            raise RollbackError(f"{ref_of(change_id)} 刚被其他操作处理，本次回滚结果未写入账本，请刷新改动日志后核对实际状态")
        dbagent_rollbacks_total.labels("failed").inc()
        await audit(actor, ACT_DBAGENT_CHANGE_ROLLBACK, {
            "change_id": change_id, "decision": "failed",
            "error": "; ".join(errors)[:200]})
        raise RollbackError(
            "组回滚有反向操作影响 0 行（其余已提交），已标记 rollback_failed，"
            "请核对改动日志后转人工" if committed else
            "组回滚失败（事务已整体回滚），已标记 rollback_failed")
    done = len(all_ops)
    flipped = await pools.execute_writer_raw(
        "UPDATE db_change_log SET status='rolled_back', rolled_back_by=$1, "
        "rolled_back_at=now() "
        "WHERE change_id = ANY($2) AND status='applied' RETURNING change_id",
        (actor, ids))
    if len(flipped) < claimed:
        raise RollbackError(f"{ref_of(change_id)} 刚被其他操作处理，本次回滚结果未写入账本，请刷新改动日志后核对实际状态")
    dbagent_rollbacks_total.labels("ok").inc()
    await audit(actor, ACT_DBAGENT_CHANGE_ROLLBACK, {
        "change_id": change_id, "decision": "rolled_back",
        "ref": ref_of(change_id), "ops": done,
        "group": len(members) if group_id else None})
    return {"ok": True, "ref": ref_of(change_id),
            "ops": done, "group": len(members) if group_id else None}


async def discard_proposed(change_id: int, actor: str,
                           owner_actor: str | None = None,
                           owner_is_admin: bool = True) -> dict:
    """废弃一笔 proposed 残留（P3-4）：awaiting_manual → discarded。

    归属校验同 execute_rollback（业主 2026-10-05 拍板）：非最高管理员只能
    废弃自己发起的 proposed。

    只翻 status（与《影子期协议》人工 psql 流程同款最小变更）；谁在何时
    废弃由 admin 审计日志承载（路由层 audit），不误用 rolled_back_* 列。
    批提案组：组内其余 awaiting_manual 行一起废弃（组是原子语义，拆着
    废弃会让下一个人误判组完整性）。非待人工条目抛 RollbackError（409）。
    """
    owned = await pools.execute_writer_raw(
        "SELECT actor, group_id FROM db_change_log "
        "WHERE change_id = $1 AND action = 'proposed' "
        "AND status = 'awaiting_manual'", (change_id,))
    if not owned:
        raise RollbackError(
            f"变更 HG{change_id} 不是待人工的 proposed 残留"
            "（已执行/已废弃/不存在），无需处理")
    if (owner_actor is not None and not owner_is_admin
            and (owned[0].get("actor") or "") != owner_actor):
        raise RollbackError(
            f"变更 HG{change_id} 由 {(owned[0].get('actor') or '未知发起人')} 登记，"
            "非最高管理员只能废弃自己发起的变更")
    rows = await pools.execute_writer_raw(
        "UPDATE db_change_log SET status = 'discarded' "
        "WHERE change_id = $1 AND action = 'proposed' "
        "AND status = 'awaiting_manual' RETURNING change_id, group_id",
        (change_id,))
    if not rows:
        # 与归属检查之间有竞态窗口：另一审批人刚废弃/翻转了状态——按 409
        # 语义收口，而不是 rows[0] IndexError 打 500（2026-10-06 审查修复）
        raise RollbackError(
            f"变更 HG{change_id} 刚被其他操作处理，请刷新改动日志后确认")
    group_id = rows[0]["group_id"]
    if group_id:
        await pools.execute_writer_raw(
            "UPDATE db_change_log SET status = 'discarded' "
            "WHERE group_id = $1 AND action = 'proposed' "
            "AND status = 'awaiting_manual'",
            (group_id,))
    return {"change_id": change_id, "status": "discarded",
            "group_id": group_id}
"""SQL 守卫（读 guard_sql / 写 guard_write_sql）：执行前的第一道关。

来源：agent_gateway data_ask_sql.py 经 DBagent 项目演化后搬回（2026-10-02）。
相对本仓 data_ask_sql 的差异：白名单来自本包 catalog（演示三表）；
新增写守卫（禁一切字面量、值只走 $N 参数、UPDATE/DELETE 必带 WHERE、
必带 RETURNING 回读）。

定位不变：本模块不是权限边界，权限边界是 dbagent_readonly/dbagent_writer
角色；守卫负责越界在执行前失败并给出操作者看得懂的原因。保守拒绝。
"""
from __future__ import annotations

import re

from .catalog import TABLES, WRITE_TABLES
from .sql_ast import SQLSecurityError, ast_available, ast_check

MAX_SQL_CHARS = 3000
MAX_ROWS = 200

_FORBIDDEN_KEYWORDS = (
    "insert", "update", "delete", "drop", "alter", "create", "truncate",
    "grant", "revoke", "copy", "vacuum", "analyze", "call", "do", "set",
    "reset", "prepare", "execute", "refresh", "reindex", "cluster",
    "comment", "lock", "notify", "listen", "unlisten", "discard",
    "explain", "into", "merge", "returning", "isolation",
)

_FORBIDDEN_FUNCTIONS = (
    "pg_sleep", "pg_sleep_for", "pg_sleep_until", "pg_read_file",
    "pg_read_binary_file", "pg_ls_dir", "pg_stat_file", "dblink",
    "lo_get", "lo_import", "lo_export", "pg_terminate_backend",
    "pg_cancel_backend", "pg_advisory_lock", "pg_advisory_unlock",
    "setseed", "nextval", "setval", "txid_current", "pg_backend_pid",
)

_FORBIDDEN_RELATIONS = ("information_schema", "pg_catalog", "pg_toast")


class SqlRejectedError(ValueError):
    """SQL 越界：调用方不得执行，直接把 reason 展示给操作者。"""


def _mask_literals(sql: str) -> str:
    """把字符串字面量与行注释替换成等长空格，供后续正则安全扫描。"""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
            continue
        if ch == "/" and sql.startswith("/*", i):
            raise SqlRejectedError("不支持块注释（/* */）")
        # E'' / U&'' 前缀：这两类串里 \', \'\' 等转义规则与下面的 '' 扫描不同，
        # 引号会算错、多遮蔽一片真实代码，使禁词/禁函数扫描漏检（10-08 台账 454）。
        # 与块注释、dollar 引用同一口径：认不出就保守拒绝，不猜。
        if ch in "eE" and sql.startswith("'", i + 1):
            raise SqlRejectedError("不支持 E'' 转义字符串")
        if ch in "uU" and sql.startswith("&", i + 1) and sql[i + 2:i + 3] == "'":
            raise SqlRejectedError("不支持 U&'' 统一字符名字符串")
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if sql.startswith("''", j):
                        j += 2
                        continue
                    break
                j += 1
            if j >= n:
                raise SqlRejectedError("单引号未闭合")
            out.append(" " * (j - i + 1))
            i = j + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _bare_identifier(raw: str) -> str:
    """去掉双引号并转小写，得到可比对的表名。"""
    return raw.strip().strip('"').lower()


_FROM_SEGMENT_END = re.compile(
    r"\b(where|group\s+by|order\s+by|having|limit|offset|join|on|union|for"
    r"|select|from)\b",
    re.IGNORECASE)

_FUNCTION_WITH_FROM = re.compile(
    r"\b(?:extract|substring|trim|btrim|position|overlay)\s*\([^()]*\)",
    re.IGNORECASE)

_CTE_ALIAS = re.compile(r"(?:\bwith\b|,)\s+([a-z_][a-z0-9_$]*)\s+as\s*\(",
                        re.IGNORECASE)


def _cte_aliases(masked: str) -> set[str]:
    """收集查询内 CTE 别名（临时关系，不按白名单核对）。"""
    return {m.group(1).lower() for m in _CTE_ALIAS.finditer(masked)}


def _referenced_tables(masked: str, cte_aliases: set[str]) -> list[str]:
    """取 FROM / JOIN 引用的表名；带 schema 前缀的形态一律拒绝。"""
    text = _FUNCTION_WITH_FROM.sub(
        lambda m: " " * len(m.group(0)), masked)
    tables: list[str] = []
    for match in re.finditer(r"\b(?:from|join)\s+([^\s,;()]+)", text, re.IGNORECASE):
        token = match.group(1)
        if "." in token:
            raise SqlRejectedError(f"不支持 schema 限定名: {token}")
        tables.append(_bare_identifier(token))
    for match in re.finditer(r"\bfrom\b", text, re.IGNORECASE):
        tail = text[match.end():]
        stop = _FROM_SEGMENT_END.search(tail)
        segment = tail[:stop.start()] if stop else tail
        for part in segment.split(",")[1:]:
            words = part.split()
            if words:
                head = _bare_identifier(words[0])
                if head:
                    tables.append(head)
    return [t for t in tables if t not in cte_aliases]


_TAIL_LIMIT_RE = re.compile(r"\blimit\s+(\d+)\b(?:\s+offset\s+\d+\b)?\s*$", re.IGNORECASE)


def _tail_limit(sql: str) -> int | None:
    """取最外层 LIMIT 的行数；没有则 None。认不出的写法返回 None（fail-safe）。"""
    match = _TAIL_LIMIT_RE.search(_mask_literals(sql))
    return int(match.group(1)) if match else None


def guard_sql(raw: str) -> str:
    """校验模型产出的只读 SQL，返回去掉尾分号的单条语句。"""
    sql = (raw or "").strip().rstrip(";").strip()
    if not sql:
        raise SqlRejectedError("SQL 为空")
    if len(sql) > MAX_SQL_CHARS:
        raise SqlRejectedError(f"SQL 超过 {MAX_SQL_CHARS} 字符")
    # dollar 引用拒、参数占位放行：run_sql 工具契约是 "$1/$2 占位 + params
    # 按序给值"（执行层 asyncpg 位置绑定），此前一刀切拒 $ 与工具说明矛盾
    # ——模型按说明写 $1 必撞墙，被迫退化成字面量 SQL（2026-10-05 对抗
    # 测试暴露）。$tag$ / $$ 形态仍然拒绝。
    if re.search(r"\$\$", sql) or re.search(r"\$[a-zA-Z_]", sql):
        raise SqlRejectedError("不支持 dollar 引用（$tag$ / $$）")
    masked = _mask_literals(sql)
    if ";" in masked:
        raise SqlRejectedError("只允许单条语句")
    lowered = masked.lower()
    if not re.match(r"^\s*(select|with)\b", lowered):
        raise SqlRejectedError("只允许 SELECT 或以 WITH 开头的只读查询")
    for word in _FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{word}\b", lowered):
            raise SqlRejectedError(f"检测到禁止的关键字: {word.upper()}")
    for relation in _FORBIDDEN_RELATIONS:
        if re.search(rf"\b{relation}\b", lowered):
            raise SqlRejectedError(f"禁止访问系统目录: {relation}")
    for func in _FORBIDDEN_FUNCTIONS:
        if re.search(rf"\b{func}\s*\(", lowered):
            raise SqlRejectedError(f"禁止调用函数: {func}")
    if re.search(r"\bfrom\s*\(", lowered):
        raise SqlRejectedError("不支持 FROM 子查询，请改成直接查白名单表")
    if re.search(r"\bjoin\s*\(", lowered):
        raise SqlRejectedError("不支持 JOIN 子查询，请改成直接关联白名单表")
    cte_aliases = _cte_aliases(lowered)
    for table in _referenced_tables(masked, cte_aliases):
        if table.startswith("pg_"):
            raise SqlRejectedError(f"禁止访问系统表: {table}")
        if table not in TABLES:
            raise SqlRejectedError(
                f"表 {table} 不在只读白名单内（可用表: {', '.join(sorted(TABLES))}）")
    tail_rows = _tail_limit(sql)
    if tail_rows is not None and tail_rows > MAX_ROWS:
        raise SqlRejectedError(f"LIMIT 不得超过 {MAX_ROWS} 行")
    # AST 静态安检员（第二道，业主 2026-10-05 拍板）：真语法树复核，
    # 拒绝异常包装回守卫口径，调用方零改动
    try:
        ast_check(sql, write=False)
    except SQLSecurityError as exc:
        raise SqlRejectedError(str(exc)) from exc
    return sql


def with_row_limit(sql: str, max_rows: int = MAX_ROWS) -> str:
    """无最外层 LIMIT 时包一层子查询强制限行（缺失才包，不重复包）。"""
    if _tail_limit(sql) is not None:
        return sql
    return f"SELECT * FROM (\n{sql}\n) AS _ask_limited LIMIT {max_rows}"


# ======================= 写通道守卫（人工审批后执行） =======================

_WRITE_VERBS = re.compile(r"^\s*(insert\s+into|update|delete\s+from)\s+([a-z_][a-z0-9_]*)",
                          re.IGNORECASE)

# 降级层专用：正则只认 _WRITE_VERBS 的首个目标表，UPDATE..FROM / DELETE..USING 的
# 第二张表要靠 AST 才查得到（10-08 台账 455）。缺 sqlglot 时一律拒收多表写形态。
_SECOND_TABLE_WRITE = re.compile(r"\b(?:from|using)\b", re.IGNORECASE)

# set 不能禁：SET 是 UPDATE 必要子句；独立 SET 语句已被动词前缀挡在外面。
_WRITE_FORBIDDEN = (
    "select", "with", "drop", "alter", "create", "truncate", "grant", "revoke",
    "copy", "vacuum", "analyze", "call", "do", "reset", "prepare",
    "execute", "refresh", "reindex", "cluster", "comment", "lock", "notify",
    "listen", "unlisten", "discard", "explain", "merge", "isolation",
)

_PLACEHOLDER_RE = re.compile(r"\$\d+")
_BARE_NUMBER_RE = re.compile(r"\b\d+\b")


def guard_write_sql(raw: str, allow_delete: bool = True) -> str:
    """校验模型提议的写 SQL：禁一切字面量（值只走 $N 参数）、白名单表、
    必带 WHERE 与 RETURNING。任何越界抛 SqlRejectedError。

    allow_delete=False 为生成前 RBAC 动词闸（生产评审⑥）：提交人无
    dbagent.write_delete 能力时 DELETE 在守卫即拒，不等人工审批兜底。
    """
    sql = (raw or "").strip().rstrip(";").strip()
    if not sql:
        raise SqlRejectedError("SQL 为空")
    if len(sql) > MAX_SQL_CHARS:
        raise SqlRejectedError(f"SQL 超过 {MAX_SQL_CHARS} 字符")
    if "'" in sql:
        raise SqlRejectedError("写 SQL 禁止字符串字面量，值必须走 $N 参数")
    if '"' in sql:
        raise SqlRejectedError("写 SQL 禁止双引号标识符，请用未加引号的表/列名")
    if ";" in sql:
        raise SqlRejectedError("只允许单条语句")
    if "/*" in sql or "--" in sql:
        raise SqlRejectedError("写 SQL 禁止注释")
    verb = _WRITE_VERBS.match(sql)
    if not verb:
        raise SqlRejectedError(
            "只允许 INSERT INTO / UPDATE / DELETE FROM 三种写语句")
    if not allow_delete and verb.group(1).lower().startswith("delete"):
        raise SqlRejectedError(
            "DELETE 需「删除数据」能力（dbagent.write_delete），当前账号未授予——"
            "可改用 UPDATE 限定范围，或联系管理员在权限管理中开通")
    table = verb.group(2).lower()
    if not ast_available() and _SECOND_TABLE_WRITE.search(sql, verb.end()):
        raise SqlRejectedError(
            "AST 静态安检员未安装（sqlglot），降级层无法核对 "
            "UPDATE..FROM / DELETE..USING 的第二张表——本条多表写已拒绝，"
            "请改成单表写")
    if table not in WRITE_TABLES:
        raise SqlRejectedError(
            f"表 {table} 不在写白名单内（可写表: {', '.join(sorted(WRITE_TABLES))}）")
    lowered = sql.lower()
    for word in _WRITE_FORBIDDEN:
        if re.search(rf"\b{word}\b", lowered):
            raise SqlRejectedError(f"写 SQL 检测到禁止的关键字: {word.upper()}")
    stripped = _PLACEHOLDER_RE.sub(" ", sql)
    if _BARE_NUMBER_RE.search(stripped):
        raise SqlRejectedError("写 SQL 禁止数字字面量，值必须走 $N 参数")
    if "$" in stripped:
        raise SqlRejectedError("检测到非法的 $ 形态（只允许 $1/$2… 参数占位符）")
    if lowered.startswith(("update", "delete")) and not re.search(r"\bwhere\b", lowered):
        raise SqlRejectedError("UPDATE/DELETE 必须带 WHERE 条件（拒绝全表写）")
    # RETURNING *（全列）：执行层直接以回读行作 after 影像（账本/反向操作依据）——
    # 模型自选列清单会导致 after 缺列、反向 SET 不全（2026-10-05 深审 T5）
    if not re.search(r"\breturning\s+\*", lowered):
        raise SqlRejectedError(
            "写语句必须以 RETURNING * 结尾（回读受影响行全列，作为证据与回滚依据）")
    for func in _FORBIDDEN_FUNCTIONS:
        if re.search(rf"\b{func}\s*\(", lowered):
            raise SqlRejectedError(f"写 SQL 禁止调用函数: {func}")
    # AST 静态安检员（第二道）：语法树级类型/表名/字面量复核
    try:
        ast_check(sql, write=True)
    except SQLSecurityError as exc:
        raise SqlRejectedError(str(exc)) from exc
    return sql


def extract_where(sql: str) -> str | None:
    """从已过写守卫的 UPDATE/DELETE 里取 WHERE 片段（供审批前的修改预览）。

    返回原始大小写的 WHERE 条件文本（不含 RETURNING）；INSERT 返回 None。
    只对 guard_write_sql 放行过的语句调用——解析假设已被守卫约束。
    """
    if sql.lstrip().lower().startswith("insert"):
        return None
    match = re.search(r"\bwhere\b(.*?)\breturning\b", sql, re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else None


# 表名提取（与写守卫同判据），预览 SQL 构造用
def write_target_table(sql: str) -> str:
    """取写语句的目标表名（小写）。调用前提：语句已过 guard_write_sql。"""
    return _WRITE_VERBS.match(sql).group(2).lower()

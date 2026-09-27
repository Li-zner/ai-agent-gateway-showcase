"""中控台「问数」表白名单 + SQL 守卫（P1 只读观测）。

定位要先说清楚：本模块**不是**权限边界。权限边界是 `rag_readonly` 角色
（授权脚本 scripts/rag_readonly_role.sql），它只有下面 7 张表的 SELECT 权、
只读事务和 5 秒语句超时。守卫的职责是让越界在**执行前**失败，并给出操作者
看得懂的原因，而不是把 `UPDATE ...` 丢给 PG 再回报一句 permission denied。

因此解析走保守拒绝：看不懂的结构（dollar quote、FROM 里调函数、系统表、
多语句）一律拒，宁可误杀也不放过。表名与列名真值来自 information_schema
（见 data_ask.py 的运行时取列），这里只登记用途说明，避免手抄字典形成第二个漂移源。
"""
from __future__ import annotations

import re

# 表白名单：key 是真实表名，value 是给模型看的用途说明（含主键等非显然事实）。
# 扩表必须回到这里加一行，是一次可评审的 diff——不用 ON ALL TABLES 就是这个原因。
ALLOWED_TABLES: dict[str, str] = {
    "payment_orders": (
        "支付订单。主键是 order_no（本表没有 id 列）；amount/fee 单位为元；"
        "status 是支付状态；paid_at 为三方回调确认时间，未支付为空"),
    "user_wallets": "用户钱包余额：余额与累计充值/消费，资金口径以本表为准",
    "requests": (
        "单次模型请求明细：耗时、输入/输出 token 数、模型名、是否成功。"
        "first_token_time 是首包时间指标，不是凭据"),
    "rag_incidents": "RAG 监测事件：诊断结论、证据、严重级别、处理状态",
    "rag_verdicts": "RAG 单次检索判定：各项分数、失败码、query_snippet（用户问题的脱敏片段）",
    "knowledge_chunks": "知识库分片：chunk_key、content 正文、valid 是失效标记（软删，false 表示已下线）",
    "semantic_cache": "语义缓存条目：query_text、答案、命中统计",
}

# 单条 SQL 与行数的硬上限：行数由 LIMIT 包装保证，长度由入参校验保证。
MAX_SQL_CHARS = 3000
MAX_ROWS = 200

# 写 / DDL / 权限 / 会话状态类关键字：词边界命中即拒。守卫目标是"拦明显越界"，
# 语义级越界交给只读角色，所以这里不做 AST 解析。
_FORBIDDEN_KEYWORDS = (
    "insert", "update", "delete", "drop", "alter", "create", "truncate",
    "grant", "revoke", "copy", "vacuum", "analyze", "call", "do", "set",
    "reset", "prepare", "execute", "refresh", "reindex", "cluster",
    "comment", "lock", "notify", "listen", "unlisten", "discard",
    "explain", "into", "merge", "returning", "isolation",
)

# 明确禁止的函数：即使有 SELECT 权也能造成时延攻击或读文件的入口。
_FORBIDDEN_FUNCTIONS = (
    "pg_sleep", "pg_sleep_for", "pg_sleep_until", "pg_read_file",
    "pg_read_binary_file", "pg_ls_dir", "pg_stat_file", "dblink",
    "lo_get", "lo_import", "lo_export", "pg_terminate_backend",
    "pg_cancel_backend", "pg_advisory_lock", "pg_advisory_unlock",
    "setseed", "nextval", "setval", "txid_current", "pg_backend_pid",
)

# 系统目录一律不可见（列真值由服务端内部查询提供，不经这条路径）。
_FORBIDDEN_RELATIONS = ("information_schema", "pg_catalog", "pg_toast")

class SqlRejectedError(ValueError):
    """SQL 越界：调用方不得执行，直接把 reason 展示给操作者。"""


def _mask_literals(sql: str) -> str:
    """把字符串字面量与注释替换成空格，长度保持不变，供后续正则安全扫描。

    为什么先遮蔽：`WHERE body = '-- 这不是注释'` 若不遮蔽会被当成注释把后半句
    丢掉，而 `'; DROP TABLE'` 会被当成多语句误拒。遮蔽后再查关键字与分号，
    两类误判同时消掉。
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        # 行注释：-- 到行尾（保留换行，避免把不同行的遮蔽串成一行）
        if ch == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
            continue
        # 块注释不支持嵌套：出现即视为看不懂的结构，交由上层拒绝
        if ch == "/" and sql.startswith("/*", i):
            raise SqlRejectedError("不支持块注释（/* */）")
        # 单引号字面量：'' 是转义，不能提前收尾
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
    name = raw.strip().strip('"')
    return name.lower()


# FROM 片段的右边界：片段的结束子句关键字。用于处理隐式交叉连接 `FROM a, b`。
# 必须含 select/from：否则 `WITH d AS (SELECT ... FROM requests) SELECT x, y
# FROM d` 里第一个 FROM 的片段会越过外层 SELECT 的逗号，把 `y` 当成第二张表。
_FROM_SEGMENT_END = re.compile(
    r"\b(where|group\s+by|order\s+by|having|limit|offset|join|on|union|for"
    r"|select|from)\b",
    re.IGNORECASE)

# 参数里合法带 FROM 关键字的函数：`extract(epoch FROM created_at)`、
# `substring(x from 1 for 2)`、`trim(both ' ' FROM body)`。提表名前必须把
# 这些函数调用整体抹掉，否则 FROM 后面的列名会被误判成表。
# 注意只抹这些函数，不能把所有括号内容抹掉——
# CTE 体 `WITH x AS (SELECT * FROM users)` 里的 users 仍要继续核对白名单。
_FUNCTION_WITH_FROM = re.compile(
    r"\b(?:extract|substring|trim|btrim|position|overlay)\s*\([^()]*\)",
    re.IGNORECASE)

# 公共表表达式别名：`WITH daily AS (...)` 以及后续的 `, hourly AS (...)`。
# 它们是查询内部产生的临时关系，不是库表，不该按白名单核对。
_CTE_ALIAS = re.compile(r"(?:\bwith\b|,)\s+([a-z_][a-z0-9_$]*)\s+as\s*\(",
                        re.IGNORECASE)


def _cte_aliases(masked: str) -> set[str]:
    """收集查询内 CTE 别名（PG 未加引号的标识符折叠为小写，故统一转小写）。"""
    return {m.group(1).lower() for m in _CTE_ALIAS.finditer(masked)}


def _referenced_tables(masked: str, cte_aliases: set[str]) -> list[str]:
    r"""取 FROM / JOIN 引用的表名；带 schema 前缀的形态一律拒绝。

    为什么分两趟：显式 JOIN 的目标能靠 `join\s+(\S+)` 直接抓到，而
    `FROM a, b` 这种隐式交叉连接的后续表名不带关键字前缀，必须先把 FROM 片段
    切出来再按逗号拆——且必须在子句边界处截断，否则 `WHERE x IN (1, 2)`
    里逗号后的 "2)" 会被当成表名而误拒。
    """
    # 抹掉 extract/substring 这类含 FROM 的函数调用，避免参数被当成表名
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


# 最外层 LIMIT 的行数判据。Postgres 里 LIMIT/OFFSET 属于最外层 SELECT，子查询与
# CTE 体里的同名子句都被括号包住、不会贴在语句末尾，所以"末尾那个 LIMIT"才是
# 真正约束返回行数的一个。写成"文本里任意一个 limit"会出两类错（2026-09-28 批1 P1-8）：
# `WITH d AS (SELECT ... LIMIT 5) SELECT ... FROM d` 既骗过了上限校验（以为已限 5 行），
# 又让 with_row_limit 以为"已经有限行"而不再包外层 → 200 行硬上限整体失效。
_TAIL_LIMIT_RE = re.compile(r"\blimit\s+(\d+)\b(?:\s+offset\s+\d+\b)?\s*$", re.IGNORECASE)


def _tail_limit(sql: str) -> int | None:
    """取最外层 LIMIT 的行数；没有则 None。字面量先遮蔽，防 `WHERE name='limit 9'` 骗判据。

    认不出的写法（`LIMIT ALL`、以行注释收尾的 `LIMIT 5 -- 说明`）一律返回 None，
    方向是 fail-safe：外层照样会被包一层 LIMIT，只会多包不会漏包。
    """
    match = _TAIL_LIMIT_RE.search(_mask_literals(sql))
    return int(match.group(1)) if match else None


def guard_sql(raw: str) -> str:
    """校验模型产出的 SQL，返回去掉尾分号的单条只读查询。

    入参：raw 模型产出的 SQL 文本；返回：可交给 run_readonly_query 的语句。
    任何越界抛 SqlRejectedError（带人话原因）。
    """
    sql = (raw or "").strip().rstrip(";").strip()
    if not sql:
        raise SqlRejectedError("SQL 为空")
    if len(sql) > MAX_SQL_CHARS:
        raise SqlRejectedError(f"SQL 超过 {MAX_SQL_CHARS} 字符")
    if "$" in sql:
        raise SqlRejectedError("不支持 dollar 引用（$tag$ / $1）")
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
    # CTE 别名放行不影响安全：体内引用的真实表名照样逐个核对，别名只是临时结果集
    for table in _referenced_tables(masked, cte_aliases):
        if table.startswith("pg_"):
            raise SqlRejectedError(f"禁止访问系统表: {table}")
        if table not in ALLOWED_TABLES:
            raise SqlRejectedError(
                f"表 {table} 不在只读白名单内（可用表: {', '.join(sorted(ALLOWED_TABLES))}）")
    # 自带 LIMIT 只有"缺失"时好包一层，写超了得直接拒：静默改写会让操作者
    # 在界面上看到的 SQL 与实际执行的不一致，排障时反而误导。
    # 只看最外层那一个（P1-8）：CTE 或子查询体内的 LIMIT 不约束本查询返回行数。
    tail_rows = _tail_limit(sql)
    if tail_rows is not None and tail_rows > MAX_ROWS:
        raise SqlRejectedError(f"LIMIT 不得超过 {MAX_ROWS} 行")
    return sql


def with_row_limit(sql: str, max_rows: int = MAX_ROWS) -> str:
    """无 LIMIT 时包一层子查询强制限行，返回最终执行的 SQL。

    为什么包而不是拒：模型经常忘记写 LIMIT，直接执行会把几十万行拉进内存；
    报错给操作者没有价值。模型自带 LIMIT 的情况已在 guard_sql 里按上限校验过，
    所以这里只需处理"缺失 LIMIT"。截断由上层按"返回行数达到上限"标记 truncated，
    不额外多取一行，也不重扫结果。

    判据与 guard_sql 必须同一个（P1-8）：原先这里看"文本任意处有没有 limit"，
    于是 CTE 体内写了 LIMIT 的无外层限行查询两头都以为安全，实际一行都没限。

    换行包裹（P1-8 连带）：以行注释收尾的语句若同行拼接，`--` 会把右括号连同
    外层 LIMIT 一起注释掉，交给数据库只剩语法错误。
    """
    if _tail_limit(sql) is not None:
        return sql
    return f"SELECT * FROM (\n{sql}\n) AS _ask_limited LIMIT {max_rows}"

"""中控台「问数」服务（P1 只读观测）：自然语言 -> SQL -> 只读执行 -> 自然语言答复。

编排顺序固定，别挪：
1. 取 schema 上下文（只读角色查 information_schema，进程内缓存）；
2. 让模型只产出 SQL；
3. 过 data_ask_sql.guard_sql 守卫（单条 / SELECT / 表白名单 / 限行）；
4. 用只读角色执行（第 3 层硬边界在 scripts/rag_readonly_role.sql）；
5. 结果列脱敏 + 行数截断；
6. 让模型就着结果行写结论。

第 2 步和第 6 步是两次独立调用：合成一次会让模型"自己查自己答"，
中间的 SQL 就没有守卫落点，也拿不到真实行数——那是本模块存在的理由。
"""
from __future__ import annotations

import json
import re
import time

from ..core.logging import setup_logging
from .data_ask_sql import ALLOWED_TABLES, MAX_ROWS, SqlRejectedError, guard_sql, with_row_limit

logger = setup_logging()

# 问数的问题与 SQL 都有硬长度上限，防把 prompt 撑爆
QUESTION_MAX_CHARS = 500
HISTORY_MAX_TURNS = 6

# schema 缓存周期：列变更只发生在迁移之后，10 分钟足够，且避免每次提问都查一遍目录
SCHEMA_CACHE_SECONDS = 600.0

# 结论阶段喂给模型的行数：200 行明细塞进 prompt 既贵又读不完，取前若干行 + 总行数
ANSWER_SAMPLE_ROWS = 30

# 脱敏列名（精确匹配，不按子串）：按子串会误伤 requests.first_token_time 这类
# 恰恰是观测要用的指标列。新表进白名单时若含凭据列，往这里加一行。
_REDACTED_COLUMNS = frozenset({
    "hashed_password", "password", "secret", "api_key", "access_token",
    "refresh_token", "phone", "password_salt",
})

_SQL_SYSTEM_PROMPT = """你是数据分析查询助手，只负责把管理员的问题翻译成一条 PostgreSQL 只读查询。

硬性要求：
1. 只输出一条 SQL，不要解释、不要 Markdown 代码块、不要注释；
2. 只能 SELECT（或以 WITH 开头的只读查询），禁止任何写操作、DDL、事务控制、系统目录；
3. 只能使用下面列出的表和列，表名区分大小写不需要，列名必须逐字一致；
4. 未显式写 LIMIT 时不要自己加超过 200 的 LIMIT；
5. 时间列各表口径不同（requests 用 start_time/end_time，其余多为 created_at），
   必须从下面的列清单里逐字取，按天统计用「时间列::date」，均为 timestamptz（UTC）；
6. 如果问题无法用这些表回答，只输出一行：UNSUPPORTED 加简短原因。

可用表与用途：
"""


def _schema_prompt(schema: dict[str, list[tuple[str, str]]]) -> str:
    """把表用途与列名拼成 prompt 片段；脱敏列不出现在上下文里，模型看不见就查不到。"""
    blocks: list[str] = []
    for table, usage in ALLOWED_TABLES.items():
        columns = [name for name, _type in schema.get(table, [])
                   if name not in _REDACTED_COLUMNS]
        if not columns:
            # 白名单表却没取到列：多半是表尚未迁移或授权漏了，明确报出来而不是静默跳过
            raise RuntimeError(f"表 {table} 未取到列信息，请检查只读角色授权")
        types = {name: dtype for name, dtype in schema.get(table, [])}
        col_text = ", ".join(f"{c} {types[c]}" for c in columns)
        blocks.append(f"- {table}：{usage}\n  列：{col_text}")
    return "\n".join(blocks)


_schema_cache: dict[str, list[tuple[str, str]]] | None = None
_schema_cache_at = 0.0


async def load_schema() -> dict[str, list[tuple[str, str]]]:
    """取白名单表的列名与类型（带 TTL 缓存）。

    为什么运行时查目录而不是抄一份字典进代码：`docs/全链路总结.md` 的表结构小节
    只覆盖支付子集且已漂移，抄进来就是第二个真值源。
    """
    global _schema_cache, _schema_cache_at
    now = time.monotonic()
    if _schema_cache is not None and now - _schema_cache_at < SCHEMA_CACHE_SECONDS:
        return _schema_cache
    from ..core.db_readonly import run_readonly_query
    rows = await run_readonly_query(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = ANY($1) "
        "ORDER BY table_name, ordinal_position",
        args=(list(ALLOWED_TABLES),),
    )
    schema: dict[str, list[tuple[str, str]]] = {t: [] for t in ALLOWED_TABLES}
    for row in rows:
        schema[row["table_name"]].append((row["column_name"], row["data_type"]))
    _schema_cache, _schema_cache_at = schema, now
    return schema


def invalidate_schema_cache() -> None:
    """迁移或授权变更后手动失效缓存（管理与测试入口）。"""
    global _schema_cache
    _schema_cache = None


def _strip_sql_fences(text: str) -> str:
    """剥掉模型偶尔仍然输出的 ```sql 围栏与前后的闲话。

    为什么还要兜这一层：提示词里已经禁止代码块，但弱模型仍会带；守卫本身
    不认识围栏，与其报错不如就地清干净。
    """
    cleaned = (text or "").strip()
    fence = re.match(r"^```[a-zA-Z]*\s*(.*?)\s*```$", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    return cleaned


def _extract_content(data: dict) -> str:
    """从 OpenAI 兼容响应里取正文；取不到返回空串由上层判失败。"""
    choices = data.get("choices") or [{}]
    message = choices[0].get("message") or {}
    return str(message.get("content") or "")


async def _call_llm(messages: list[dict], max_tokens: int) -> str:
    """单次非流式调用：flash 优先（问数是轻量结构化任务），失败抛错由上层转 5xx。

    空正文按失败处理：flash 会先把预算花在 reasoning_content 上，正文可能是
    空串。当成功返回会让 SQL 阶段报出"守卫拒绝: SQL 为空"这种误导结论——
    本机 2026-09-23 E2E 实测踩过（max_tokens=600 时结论段就是空的）。
    """
    from ..core.config import HTTP_TIMEOUT_MEDIUM
    from .chat_support import post_chat_completion
    ok, data = await post_chat_completion(
        {"model": None, "messages": messages, "temperature": 0.0,
         "max_tokens": max_tokens},
        timeout=HTTP_TIMEOUT_MEDIUM, prefer_flash=True)
    if not ok:
        raise RuntimeError("上游 LLM 不可用")
    content = _extract_content(data)
    if not content.strip():
        raise RuntimeError("上游 LLM 返回空内容，请重试或缩小问题范围")
    return content


async def generate_sql(question: str, history: list[dict],
                       schema: dict[str, list[tuple[str, str]]]) -> str:
    """让模型产出 SQL 并过守卫；返回守卫放行后的单条语句。"""
    messages = [
        {"role": "system", "content": _SQL_SYSTEM_PROMPT + _schema_prompt(schema)},
        *history,
        {"role": "user", "content": f"管理员问题：{question}"},
    ]
    # 1500 而非 800：reasoning_content 与正文共用这段预算，给小了正文会被推理吃空
    # （口径同 rag_admin 的 /ask，那里从 5s 短超时提到 MEDIUM + 2000 才稳定）
    raw = await _call_llm(messages, max_tokens=1500)
    sql = _strip_sql_fences(raw)
    if sql.upper().startswith("UNSUPPORTED"):
        raise SqlRejectedError(f"模型判断该问题无法用现有白名单回答: {sql[:160]}")
    return guard_sql(sql)


def _redact_rows(columns: list[str], rows: list[dict]) -> tuple[list[str], list[dict]]:
    """结果出口再脱敏一次：模型可能查出预期之外的列名形态。"""
    keep = [c for c in columns if c not in _REDACTED_COLUMNS]
    dropped = [c for c in columns if c in _REDACTED_COLUMNS]
    if dropped:
        logger.info("问数结果脱敏列: %s", ",".join(dropped))
    return keep, [{c: row.get(c) for c in keep} for row in rows]


def _json_safe(value: object) -> object:
    """把 Decimal / datetime / UUID 等转成可序列化形态（前端直接渲染）。"""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    return str(value)


async def answer_from_rows(question: str, sql: str, columns: list[str],
                           rows: list[dict]) -> str:
    """就着真实结果行让模型写结论；这一步失败不该让整次查询失败。"""
    sample = rows[:ANSWER_SAMPLE_ROWS]
    payload = json.dumps(
        {"columns": columns, "row_count": len(rows), "rows": sample},
        ensure_ascii=False, default=str)
    messages = [
        {"role": "system", "content": (
            "你是运维数据分析助手。只依据给出的查询结果回答，不得编造数字。"
            "结果被截断时要说明只看了前若干行。用中文，两三句话。")},
        {"role": "user", "content": (
            f"问题：{question}\n执行的 SQL：{sql}\n查询结果（JSON）：{payload}")},
    ]
    try:
        return await _call_llm(messages, max_tokens=600)
    except Exception as exc:
        logger.warning("问数结论生成失败（结果仍返回）: %s", type(exc).__name__)
        return "（结论生成失败，以下为原始查询结果）"


def _dedupe_columns(names) -> list[str]:
    """SELECT 重名列（u1.name, u2.name 忘加别名）时后列加 _N 后缀。

    asyncpg 的 keys() 原样返回重名、r[c] 恒取后值，直接 `{c: r[c] for c in columns}`
    会让前列的值被静默覆盖（2026-09-28 批1 P2-12）。列名唯一时原样返回，行为不变。
    """
    if len(set(names)) == len(names):
        return list(names)
    seen: dict[str, int] = {}
    out = []
    for n in names:
        seen[n] = seen.get(n, 0) + 1
        out.append(n if seen[n] == 1 else f"{n}_{seen[n]}")
    return out


async def ask_data(question: str, history: list[dict] | None = None) -> dict:
    """问数主流程。入参问题文本与可选历史；返回 sql/columns/rows/truncated/answer。

    失败语义：schema 取不到、SQL 被守卫拒、执行报错，都抛给调用方转 5xx，
    绝不返回"成功 + 空结果"骗前端。
    """
    text = (question or "").strip()[:QUESTION_MAX_CHARS]
    if not text:
        raise ValueError("question 不能为空")
    schema = await load_schema()
    sql = await generate_sql(text, (history or [])[-HISTORY_MAX_TURNS:], schema)
    executed_sql = with_row_limit(sql, MAX_ROWS)

    from ..core.db_readonly import run_readonly_query
    started = time.monotonic()
    records = await run_readonly_query(executed_sql)
    elapsed_ms = int((time.monotonic() - started) * 1000)

    if not records:
        return {"sql": sql, "columns": [], "rows": [], "row_count": 0,
                "truncated": False, "elapsed_ms": elapsed_ms,
                "answer": "查询成功但没有匹配数据。"}
    columns = _dedupe_columns(records[0].keys())
    # 按 values() 的位置序 zip 列名：重名时 r[c] 取后值，位置序才能拿到每一列自己的值
    rows = [dict(zip(columns, (_json_safe(v) for v in r.values()))) for r in records]
    truncated = len(rows) >= MAX_ROWS
    columns, rows = _redact_rows(columns, rows)
    answer = await answer_from_rows(text, sql, columns, rows)
    return {"sql": sql, "columns": columns, "rows": rows,
            "row_count": len(rows), "truncated": truncated,
            "elapsed_ms": elapsed_ms, "answer": answer}

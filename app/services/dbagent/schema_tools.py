"""schema 上下文：运行时从 information_schema 取白名单表的列（TTL 缓存）。

来源：DBagent 项目 schema_tools.py 搬入（同 data_ask.load_schema 的立场）：
列名运行时查目录而不是抄字典，杜绝第二真值源漂移。模型 SQL 禁查系统目录
（守卫拦截）；本模块的目录查询是包内受信常量语句、参数绑定、走只读通道。
"""
from __future__ import annotations

import re
import time

from .catalog import TABLES

SCHEMA_CACHE_SECONDS = 600.0
# P2-4 按需加载：白名单表数超过该上限才开始裁剪（目录 ≤ 上限时全量展开，
# 行为与裁剪机制引入前完全一致）；上限可随提示词预算调
SCHEMA_TABLE_LIMIT = 10

_CJK = re.compile(r"[\u4e00-\u9fff]+")
_ASCII_WORD = re.compile(r"[a-z_]+")

_REDACTED_COLUMNS = frozenset({
    "hashed_password", "password", "secret", "api_key", "access_token",
    "refresh_token", "phone", "password_salt",
})

_schema_cache: dict[str, list[tuple[str, str]]] | None = None
_schema_cache_at = 0.0


async def load_schema(fetch) -> dict[str, list[tuple[str, str]]]:
    """取白名单表的列名与类型；fetch 是只读执行函数（fetch_dicts），便于注入。"""
    global _schema_cache, _schema_cache_at
    now = time.monotonic()
    if _schema_cache is not None and now - _schema_cache_at < SCHEMA_CACHE_SECONDS:
        return _schema_cache
    rows = await fetch(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = ANY($1) "
        "ORDER BY table_name, ordinal_position",
        params=(list(TABLES),),  # ANY($1) 绑定单个数组参数
    )
    schema: dict[str, list[tuple[str, str]]] = {t: [] for t in TABLES}
    for row in rows:
        schema[row["table_name"]].append((row["column_name"], row["data_type"]))
    _schema_cache, _schema_cache_at = schema, now
    return schema


def invalidate_schema_cache() -> None:
    """DDL 后手动失效缓存（管理与测试入口）。"""
    global _schema_cache
    _schema_cache = None


def _cjk_bigrams(text: str) -> set[str]:
    """中文连续段的二元组（免分词的相关度信号）；ASCII 词整体小写参与。"""
    grams: set[str] = set()
    for run in _CJK.findall(text):
        grams |= {run[i:i + 2] for i in range(len(run) - 1)}
    grams |= set(_ASCII_WORD.findall(text.lower()))
    return grams


def select_tables_for_question(question: str,
                               limit: int = SCHEMA_TABLE_LIMIT) -> list[str]:
    """按问题相关性选表（P2-4 按需加载）。

    目录 ≤ limit 时全量返回（现状行为，评测不受扰动）；超出才裁剪：
    表名命中强加权，其次描述二元组重叠数。零命中回退全量（能力问询等
    泛化问题需要完整目录视野）。
    """
    names = list(TABLES)
    if len(names) <= limit:
        return names
    q_grams = _cjk_bigrams(question)
    scored: list[tuple[int, str]] = []
    for name, usage in TABLES.items():
        score = 5 if name in question else 0
        score += len(q_grams & _cjk_bigrams(usage))
        scored.append((score, name))
    scored.sort(key=lambda p: (-p[0], p[1]))
    picked = [name for score, name in scored[:limit] if score > 0]
    return picked or names


def schema_prompt(schema: dict[str, list[tuple[str, str]]],
                  question: str | None = None) -> str:
    """把表用途与列名拼成提示词片段；脱敏列不出现在模型上下文。

    P2-4：带 question 且目录超限时只展开相关表（select_tables_for_question），
    其余表只留一行名录——模型知道存在哪些表，但无法凭空猜列（如实向用户
    说明并建议细化问题重提，而不是编造列名硬查）。
    """
    blocks: list[str] = []
    selected = (select_tables_for_question(question)
                if question is not None else list(TABLES))
    for table in selected:
        usage = TABLES[table]
        columns = [name for name, _type in schema.get(table, [])
                   if name not in _REDACTED_COLUMNS]
        if not columns:
            raise RuntimeError(f"表 {table} 未取到列信息，请检查表与只读角色授权")
        types = {name: dtype for name, dtype in schema.get(table, [])}
        col_text = ", ".join(f"{c} {types[c]}" for c in columns)
        blocks.append(f"- {table}：{usage}\n  列：{col_text}")
    rest = [t for t in TABLES if t not in selected]
    if rest:
        blocks.append(
            f"- 另有 {len(rest)} 张白名单表未展开（与本次问题相关性低）："
            + "、".join(rest)
            + "。它们仍可查询但列结构未提供；如需其数据请如实说明无法确认列结构，"
              "建议用户细化问题后重新提交，不要猜列名硬查。")
    return "\n".join(blocks)

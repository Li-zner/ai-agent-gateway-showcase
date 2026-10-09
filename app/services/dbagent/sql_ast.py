"""SQL AST 静态安检员（2026-10-05 业主拍板引入 sqlglot）。

角色定位（业主原话）：**只读的静态安检员**，不是改写 SQL 的大脑——不参与
NL2SQL、不改写语句，只在正则守卫之后加一道基于真语法树的复核：
①语句类型白名单：解析出 DROP/ALTER/TRUNCATE 等直接代码级拒绝（守卫在
  审批门之前，连审批界面都不弹）；
②表名校验：schema 限定名与白名单外表拦下（含正则难覆盖的变形）；
③写语句字面量复核：AST 级字面量检查兜正则绕过（读语句允许字面量，
  与正则守卫口径一致——读通道的值可以内联，写通道值必须 $N 参数）。

sqlglot 缺失时整体跳过（不报错）——本模块是第二道安检，第一道正则守卫与
库级角色权限边界不依赖它；解析失败按拒绝处理（保守，与守卫同口径）。
异常分层：本模块抛 SQLSecurityError，sql_guard 包装成 SqlRejectedError
（避免 import 环，调用方只认守卫异常一种口径）。
"""
from __future__ import annotations

import logging

logger = logging.getLogger("rag_console.dbagent")

try:
    import sqlglot
    from sqlglot import exp
except ImportError:  # pragma: no cover — 依赖未安装时降级跳过
    sqlglot = None

from .catalog import TABLES, WRITE_TABLES

# 只读安检员放行的语句类型（与守卫动词口径一致）
_ALLOWED_KINDS = {"select", "insert", "update", "delete"}


class SQLSecurityError(ValueError):
    """AST 安检拒绝（sql_guard 负责包装为 SqlRejectedError）。"""


_missing_warned = False


def _warn_missing_once() -> None:
    """sqlglot 缺失只告警一次（进程级）；安检员降级跳过，正则守卫仍在。"""
    global _missing_warned
    if not _missing_warned:
        _missing_warned = True
        logger.warning("sqlglot 未安装，AST 静态安检员降级跳过"
                       "（pip install sqlglot 启用；正则守卫不受影响）")


def ast_available() -> bool:
    """AST 安检员是否在场（sqlglot 已安装）。

    守卫的降级层要靠它判断是否需要补做正则查不了的形态（10-08 台账 455）。
    """
    return sqlglot is not None


def ast_check(sql: str, *, write: bool) -> None:
    """语法树复核一条 SQL；不通过抛 SQLSecurityError。

    write=True 走写白名单并追加字面量禁令；write=False 走读白名单。
    """
    if sqlglot is None:
        _warn_missing_once()
        return
    try:
        statements = sqlglot.parse(sql, read="postgres")
    except Exception as exc:  # noqa: BLE001 — 解析不动即拒绝（保守）
        raise SQLSecurityError(
            f"AST 安检：SQL 解析失败（{type(exc).__name__}）") from exc
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise SQLSecurityError("AST 安检：只允许单条语句")
    root = statements[0]
    kind = root.key.lower()
    if kind not in _ALLOWED_KINDS:
        raise SQLSecurityError(
            f"AST 安检：语句类型 {kind.upper()} 不在允许集"
            "（SELECT/INSERT/UPDATE/DELETE），疑似危险操作已拦截")
    ctes = {c.alias for c in root.find_all(exp.CTE)}
    tables_allowed = set(WRITE_TABLES) if write else set(TABLES)
    for table in root.find_all(exp.Table):
        if table.db or table.catalog:
            raise SQLSecurityError(
                f"AST 安检：不支持 schema 限定名 {table.sql()}")
        if table.name in ctes:
            continue
        if table.name not in tables_allowed:
            raise SQLSecurityError(
                f"AST 安检：表 {table.name} 不在白名单内")
    if write:
        for node in root.find_all(exp.Literal):
            if not (node.is_string or node.is_number):
                continue
            if isinstance(node.parent, exp.Parameter):
                continue  # $N 占位符在 sqlglot 里是 Parameter 包着的数字字面量
            raise SQLSecurityError(
                "AST 安检：写语句含字面量，值必须走 $N 参数")

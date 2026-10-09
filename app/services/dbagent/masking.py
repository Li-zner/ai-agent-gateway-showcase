"""LLM 出云脱敏：查询结果进模型上下文前按列规则遮蔽真实值。

业主口径（2026-10-03）：送到云端的内容脱敏——键名可上、敏感值不上。

层级定位（2026-10-04 P1-4 起）：敏感列的真值边界已下推到库级
（readonly 角色列级 GRANT + demo_users_masked 脱敏出口视图，见
DBagent/scripts/readonly_role.sql）；本模块保留为**第二层**——
新敏感列登记到 MASK_RULES 但角色 GRANT 尚未收紧的窗口期内仍生效，
漏登第二层不再等于泄真值。三种规则：
- redact：值替换为固定掩码 ***（模型知道"有这列"但看不到值）；
- pseudonym：值替换为稳定代号 U1/U2…（同一任务内同值同代号，模型可计数、
  可对齐引用，但拿不到真值；代号映射只留在进程内存，永不发云）；
- keep：默认，不遮。

边界与取舍（ponytail 天花板已标注）：
- 证据链登记的是遮蔽后的行——报告引用代号，校验天然自洽；
- 变更账本（人审通道）登记真实行——回滚与审批预览给人看，人可以见真值；
- 写操作 params 由模型提供：模型只见代号时无法写出该列真值——写敏感列
  属于反模式，正好被结构性挡住。
"""
from __future__ import annotations

import re

from .catalog import TABLES

# 遮蔽规则（按表按列）：第二层防御——真值边界在库级（列级 GRANT + masked
# 视图）。新表接MASK_RULES 时同步收紧 readonly_role.sql 的列级 GRANT，
# 两处都登记才完整（见《影子期协议.md》扩表三文件）。
MASK_RULES: dict[str, dict[str, str]] = {
    "demo_users": {
        "phone": "pseudonym",       # 手机号：代号化（可引用计数，不可见真值）
        "fake_password": "redact",  # 假密码：纯掩码（本就不该进任何上下文）
    },
    # 模拟业务表（影子期实战，2026-10-04）：与 readonly_role.sql 列级 GRANT
    # 两处同步（协议扩表三文件清单）。库级已是第一道（phone 未授出），
    # 本条是第二层（新敏感列 GRANT 收紧前的窗口期仍生效）
    "biz_customers": {
        "phone": "pseudonym",
    },
}

_REDACTED = "***"

# `源列 AS 别名`（2026-10-07 C3/P1-9）：行键是别名时按列名字面匹配会整列漏遮，
# `SELECT phone AS p` 就把真手机号直送模型。表达式包裹（substr(phone,1,3) AS p）
# 与无 AS 的引号别名（phone "p"）本层认不出，属残余——第一道库级列级 GRANT 才是
# 真值边界（见模块 docstring 的层级定位）。
_ALIAS_RE = re.compile(r"\b([A-Za-z_]\w*)\s+AS\s+([A-Za-z_]\w*)", re.IGNORECASE)


def _select_aliases(sql: str) -> dict[str, str]:
    """`col AS alias` → {别名小写: 源列小写}。"""
    return {a.lower(): c.lower() for c, a in _ALIAS_RE.findall(sql or "")}


class RowMasker:
    """任务级遮蔽器：持有代号映射（同任务内稳定），遮蔽结果行。"""

    def __init__(self) -> None:
        self._alias: dict[object, str] = {}
        self._next = 1

    def _pseudonym(self, value: object) -> str:
        if value not in self._alias:
            self._alias[value] = f"U{self._next}"
            self._next += 1
        return self._alias[value]

    def mask_rows(self, table: str, rows: list[dict],
                  aliases: dict[str, str] | None = None) -> list[dict]:
        """按表的列规则遮蔽行；未登记的表/列原样返回。

        aliases 把指向敏感列的别名键一起遮（P1-9）。
        """
        rules = MASK_RULES.get(table)
        if not rules or not rows:
            return rows
        aliases = aliases or {}
        masked = []
        for row in rows:
            out = dict(row)
            # 行键按大小写不敏感命中（引号别名 "P"、PHONE 这类同样算敏感列）
            key_index = {k.lower(): k for k in out}
            for col, rule in rules.items():
                names = [col] + [a for a, src in aliases.items() if src == col]
                for name in names:
                    key = key_index.get(name)
                    if key is not None and out[key] is not None:
                        out[key] = (self._pseudonym(out[key]) if rule == "pseudonym"
                                    else _REDACTED)
            masked.append(out)
        return masked


def mask_rows_for_sql(sql: str, rows: list[dict], masker: RowMasker) -> list[dict]:
    """从 SQL 文本推断涉及的表白名单表，合并应用各表遮蔽规则。

    多表 JOIN 时各表规则并集生效；列名撞车属演示库低概率场景，
    同名列一起遮（宁可多遮）。
    """
    aliases = _select_aliases(sql)
    for table in TABLES:
        # IGNORECASE（P1-9）：旧写法只认小写表名，`FROM Biz_Customers` 直接旁路
        if re.search(rf"\b{re.escape(table)}\b", sql or "", re.IGNORECASE):
            rows = masker.mask_rows(table, rows, aliases)
    return rows

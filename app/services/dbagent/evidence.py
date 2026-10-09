"""证据链：登记每条查询，校验报告里每个数字可溯源。

来源：DBagent 项目 evidence.py 原样搬入（2026-10-02）。
报告正文每个数字必须以 [E#] 引用证据且真实出现在证据结果/参数里；
模型没查过的数不许出现——对数字幻觉的结构性防线。
"""
from __future__ import annotations

import json
import re

_CITATION_RE = re.compile(r"\[E(\d+)\]")
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


class Evidence:
    """一条已执行的查询证据：SQL + 结果行；kind 区分读/写。"""

    def __init__(self, eid: str, sql: str, rows: list[dict],
                 kind: str = "read", params: list | None = None):
        self.id, self.sql, self.rows = eid, sql, rows
        self.kind, self.params = kind, params

    def rows_json(self) -> str:
        """比对基底：读证据就是行；写证据并入执行参数（写数值可溯源到参数）。"""
        if self.params is None:
            return json.dumps(self.rows, ensure_ascii=False, default=str)
        return json.dumps({"rows": self.rows, "params": self.params},
                          ensure_ascii=False, default=str)

    def summary(self) -> str:
        """注入对话上下文的摘要：行数 + 样例，防大结果撑爆 prompt。"""
        sample = self.rows[:5]
        payload = json.dumps(sample, ensure_ascii=False, default=str)
        tag = "写" if self.kind == "write" else "读"
        extra = f"，参数 {json.dumps(self.params, ensure_ascii=False)}" \
            if self.params is not None else ""
        return (f"[{self.id}]（{tag}）行数 {len(self.rows)}{extra}，样例：{payload}")


class EvidenceLog:
    """证据登记簿：任务内单调递增登记。

    task_text：任务原文——用户提问里出现的数字（"最近 14 天"的 14）在报告
    中复述不算编造，校验时直接豁免。
    """

    def __init__(self, task_text: str = "") -> None:
        self._items: list[Evidence] = []
        self._task_numbers = set(
            re.findall(r"\d[\d,]*(?:\.\d+)?", task_text or ""))

    def register(self, sql: str, rows: list[dict], kind: str = "read",
                 params: list | None = None) -> str:
        """登记一条证据，返回引用 id（E1、E2…）。"""
        eid = f"E{len(self._items) + 1}"
        self._items.append(Evidence(eid, sql, rows, kind, params))
        return eid

    def get(self, eid: str) -> Evidence | None:
        """按 id 取证据；不存在返回 None。"""
        return next((e for e in self._items if e.id == eid), None)

    def transcript(self) -> str:
        """全部证据摘要（观察文本）。"""
        if not self._items:
            return "（暂无证据，请先 run_sql）"
        return "\n".join(e.summary() for e in self._items)

    def appendix_markdown(self) -> str:
        """代码生成的证据附录：SQL 与行数，不经过模型。"""
        lines = ["## 证据附录"]
        for e in self._items:
            tag = "写" if e.kind == "write" else "读"
            lines.append(f"- [{e.id}]（{tag}）行数 {len(e.rows)}")
            lines.append(f"  ```sql\n  {e.sql}\n  ```")
        lines.append("---")
        lines.append("边界声明：写操作均经人工审批后执行；建议动作不经系统自动执行。")
        return "\n".join(lines)


def _evidence_numbers(text: str) -> list[float]:
    """从证据 JSON 文本里提取全部数字（含千分位），供派生验证重算。"""
    return [float(m.replace(",", ""))
            for m in re.findall(r"\d[\d,]*(?:\.\d+)?", text)]


def _derivable(target: float, pool: list[float]) -> bool:
    """派生验证：target 能否由证据数字的两元差/和重算出来（容差 0.011）。

    ponytail: 只放行两元运算（差值/合计）——比率、平均等复杂派生仍拒，
    升级路径 = 显式白名单公式。此前一刀切禁派生把"环比多了 36 万"这类
    基本分析表达全堵死（例13教训）。
    """
    for i, a in enumerate(pool):
        for j, b in enumerate(pool):
            if i == j:
                continue
            if abs(a - b - target) <= 0.011 or abs(a + b - target) <= 0.011:
                return True
    return False


def check_citations(text: str, log: EvidenceLog) -> list[str]:
    """校验报告正文：含数字的行必须有引用，且数字能在被引证据里找到。

    「建议动作」小节起豁免：那是给人工看的文字建议（"先挑 3 个样例"这类
    序号/量词不是数据结论），曾把否决后的收尾报告整体卡死（2026-10-03 例9）。
    """
    problems: list[str] = []
    in_suggestions = False
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip():
            continue
        if re.match(r"^#{1,6}\s*建议动作", raw_line.strip()):
            in_suggestions = True
            continue
        if in_suggestions and re.match(r"^#{1,6}\s", raw_line.strip()):
            in_suggestions = False  # 进入下一个小节则恢复校验
        if in_suggestions:
            continue
        citations = {f"E{m.group(1)}" for m in _CITATION_RE.finditer(raw_line)}
        stripped = _CITATION_RE.sub("", raw_line)
        # 标识符豁免：裸 E#、订单号（ORD-1011）、HG 编号（HG55/HG-55）这类
        # 业务标识符里的数字不是数据，先剥掉再提数字
        stripped = re.sub(r"\bE\d+\b", "", stripped)
        stripped = re.sub(r"\b[A-Z]{2,}-?\d+\b", "", stripped)
        tokens = [t for t in _NUMBER_RE.findall(stripped)
                  if t not in log._task_numbers and t.replace(",", "") not in log._task_numbers]
        if not tokens:
            continue
        if not citations:
            problems.append(f"第 {lineno} 行含数字但未标注 [E#] 引用：{raw_line.strip()}")
            continue
        for token in tokens:
            variants = {token, token.replace(",", "")}
            found = any(
                v in log.get(eid).rows_json()
                for eid in citations if log.get(eid)
                for v in variants)
            if not found:
                # 派生验证：差值/合计能由所引证据数字重算即放行
                try:
                    target = float(token.replace(",", ""))
                except ValueError:
                    target = None
                pool: list[float] = []
                for eid in citations:
                    ev = log.get(eid)
                    if ev:
                        pool.extend(_evidence_numbers(ev.rows_json()))
                if target is not None and _derivable(target, pool):
                    continue
                problems.append(
                    f"第 {lineno} 行数字 {token} 未出现在所引证据"
                    f"（{', '.join(sorted(citations))}）的结果里——"
                    "只允许使用查询结果原值或可由其重算的差值/合计")
    return problems


class ReportRejected(RuntimeError):
    """报告未通过证据链校验，禁止出稿；problems 携带人话原因。"""

    def __init__(self, problems: list[str]):
        super().__init__("报告未通过证据链校验: " + "；".join(problems))
        self.problems = problems

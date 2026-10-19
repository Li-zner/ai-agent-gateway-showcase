# -*- coding: utf-8 -*-
"""评测集防泄漏闸（SOP 批次 B / B3）：新语料入库前与 holdout 评测用例对撞。

为什么要有这条：清洗越成功、语料越像评测集，R@5 / MRR 就越虚——指标测的是
「检索能不能找到考卷上写过的内容」，而不是「检索好不好」。一旦某轮清洗把评测提问、
人工标注或按答案改写的条文灌进库，纸面上那两个数字就失去意义，而且是**单向变好**，
常规回归完全看不出来。

判定口径（与直觉相反的地方都写清）：
- 法条原文与提问共享条号、关键词属正常，**不算泄漏**——语料本来就该含 gold 条文；
- 提问文本原样出现在条目里算**强泄漏**（拦下不入库）；
- 提问字符大部分按序出现在条目里算**疑似**（只报不拦，交人判读）。
"""
import re
from collections import Counter
from difflib import SequenceMatcher
from typing import Iterable

# 归一化剥掉的字符：空白（含全角空格、Ideographic Space）与中英文标点。
# 保留汉字、数字与字母——比较只在「有效内容字符」上做，否则排版差异会淹没真实重合。
_STRIP_RE = re.compile(
    "[\\s\\u3000\\u00a0，。、；：？！“”‘’\"'（）()《》〈〉【】〔〕·…—\\-~～｜/\\\\*]+")

# 提问短于这个长度时不参与对撞：单条「怎么办」这类碎片会与任何文本重叠，纯误报。
_MIN_QUERY_CHARS = 12

# 字符集预筛：提问里按序可对齐的字符比例先到这个门槛，才值得跑 difflib。
# 这是性能护栏而非判定门槛——1260 条 × 45 例逐个跑 SequenceMatcher 会把闸拖成分钟级。
_PREFILTER_OVERLAP = 0.6

# 疑似门槛：提问字符按序被条目覆盖的比例（coverage），不含顺序错乱的乱序重合。
_SUSPECT_COVERAGE = 0.85


def normalize(text: str) -> str:
    """比较用的归一形态：去空白与标点，不做简繁/同义替换（那会把真实差异也抹平）。"""
    return _STRIP_RE.sub("", text or "")


def _coverage(query: str, content: str) -> float:
    """提问字符被条目按序覆盖的比例（difflib 匹配块长度之和 / 提问长度）。"""
    if not query:
        return 0.0
    matcher = SequenceMatcher(None, content, query)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return matched / len(query)


def _quick_overlap(q_counter: Counter, content_counter: Counter, query_len: int) -> float:
    """字符集层面的粗重合，必要非充分——只用来把明显无关的条目挡在 difflib 之前。"""
    if query_len == 0:
        return 0.0
    common = sum(min(n, content_counter.get(ch, 0)) for ch, n in q_counter.items())
    return common / query_len


def check_leakage(chunks: Iterable[dict], holdout_cases: Iterable[dict], *,
                  suspect_coverage: float = _SUSPECT_COVERAGE) -> dict:
    """新语料 vs holdout 用例逐条对撞，返回强泄漏（应剔）与疑似（只报）。

    chunks 每项需含 chunk_key 与 content；holdout_cases 每项需含 id 与 query
    （形态与 `tests/run_retrieval_eval.py:67 load_cases_and_labels("holdout")` 一致）。
    本函数**不改动入参**，剔除由调用方按 removed 名单执行——闸不许偷偷少写几条。
    """
    cases = []
    for case in holdout_cases:
        query = normalize(case.get("query", ""))
        if len(query) >= _MIN_QUERY_CHARS:
            cases.append((case.get("id", ""), query, Counter(query)))
    removed, suspects = [], []
    scanned = 0
    for chunk in chunks:
        content = normalize(chunk.get("content", ""))
        scanned += 1
        if not content:
            continue
        counters = Counter(content)
        for case_id, query, q_counter in cases:
            if _quick_overlap(q_counter, counters, len(query)) < _PREFILTER_OVERLAP:
                continue
            if query and query in content:
                removed.append({"chunk_key": chunk.get("chunk_key", ""),
                                "case_id": case_id, "rule": "query_in_chunk",
                                "coverage": 1.0})
                break
            coverage = _coverage(query, content)
            if coverage >= suspect_coverage:
                suspects.append({"chunk_key": chunk.get("chunk_key", ""),
                                 "case_id": case_id, "rule": "coverage",
                                 "coverage": round(coverage, 4)})
                break
    return {
        "passed": not removed,
        "reasons": [f"强泄漏 {len(removed)} 条：提问文本原样出现在语料条目里"] if removed else [],
        "removed": removed,
        "suspect": suspects,
        "scanned": scanned,
        "compared_cases": len(cases),
    }


def load_holdout_cases() -> tuple[list[dict], dict]:
    """取真实 holdout 用例与标注，评测脚本是唯一切分权威，本模块不自定 45 条。"""
    from ..rag_quality_gate import _eval_module

    cases, labels, _ = _eval_module().load_cases_and_labels("holdout")
    return cases, labels

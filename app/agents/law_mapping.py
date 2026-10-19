"""
法律依据纠正映射表
加载 tests/civil_supplement_cases.txt 中的 40 条常见法律误区，
在民法典知识库检索前，先判断用户问题是否属于其他法律领域。
"""
import csv
import re
import threading
from pathlib import Path
from typing import Optional
from ..core.logging import setup_logging

logger = setup_logging()

# 映射表路径
_MAPPING_FILE = Path(__file__).parent.parent.parent / "tests" / "civil_supplement_cases.txt"

# 在内存中缓存解析后的映射表
_LAW_MAPPING_CACHE: Optional[list[dict]] = None

# 映射表加载锁（P1 #8/#44：并发首次加载防重复读盘；防御未来 async 化）
# 必须可重入（2026-10-07 C2/P1-4）：reload_mapping 持本锁后再调 _load_mapping，
# 而那一刻缓存已被置 None，_load_mapping 会再次 acquire 同一把锁——
# threading.Lock 非重入即同线程自死锁（09-08 pytest 收集挂死 5 分钟+ 的真根因）。
_LOAD_LOCK = threading.RLock()


def _load_mapping() -> list[dict]:
    """加载并解析映射表（懒加载 + 锁保护 + 文件缺失优雅降级，P0 #59）"""
    global _LAW_MAPPING_CACHE
    if _LAW_MAPPING_CACHE is not None:
        return _LAW_MAPPING_CACHE
    with _LOAD_LOCK:
        # 双重检查：避免拿锁后重复加载
        if _LAW_MAPPING_CACHE is not None:
            return _LAW_MAPPING_CACHE
        if not _MAPPING_FILE.exists():
            # 文件缺失：返回空映射表，不阻塞应用启动（P0 #59）
            logger.warning(f"法律映射表文件缺失，映射表为空: {_MAPPING_FILE}")
            _LAW_MAPPING_CACHE = []
            return _LAW_MAPPING_CACHE

        entries = []
        with open(_MAPPING_FILE, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter="\t")
            for row in reader:
                # 提取关键词（从场景描述中拆分）
                scenario = row.get("用户常见问法/场景", "").strip()
                law = row.get("实际法律依据", "").strip()
                citation = row.get("正确引用索引", "").strip()
                if not scenario:
                    continue
                # 生成关键词列表：拆分场景描述中的关键词
                keywords = _extract_keywords(scenario)
                entries.append({
                    "id": len(entries) + 1,
                    "scenario": scenario,
                    "law": law,
                    "citation": citation,
                    "keywords": keywords,
                    "wrong_area": row.get("常误认为《民法典》条文", "").strip(),
                })

        _LAW_MAPPING_CACHE = entries
    return _LAW_MAPPING_CACHE


def _extract_keywords(text: str) -> list[str]:
    """从场景描述中提取关键词"""
    # 去除标点符号，按常见分隔符拆分
    text = re.sub(r"[/、，。；：""''（）()/·]", " ", text)
    words = text.split()
    # 过滤过短或常见无意义词
    result = []
    for w in words:
        w = w.strip()
        if len(w) >= 2 and w not in ("什么", "怎么", "如何", "哪些", "是否", "可以", "需要", "相关", "请问"):
            result.append(w)
    return result


def _calculate_match_score(query: str, keywords: list[str]) -> float:
    """计算用户问句与映射表条目的匹配得分（支持子串匹配和字符级重合）"""
    if not keywords:
        return 0.0

    # 1) 精确子串匹配：关键词在问句中出现
    exact_matched = sum(1 for kw in keywords if kw in query)

    # 2) 子词匹配：关键词中的每个 ≥2 字的片段在问句中出现
    sub_matched = 0
    sub_total = 0
    for kw in keywords:
        if len(kw) <= 2:
            continue
        # 将关键词拆成 2~4 字的滑动窗口片段
        for win_size in range(4, 1, -1):
            for i in range(len(kw) - win_size + 1):
                sub = kw[i:i+win_size]
                if len(sub) < 2:
                    continue
                sub_total += 1
                if sub in query:
                    sub_matched += 1

    # 3) 综合得分
    if exact_matched >= 2:
        # 多个关键词精确命中 → 强匹配
        return 1.0 + (exact_matched / len(keywords) * 0.2)
    elif exact_matched == 1 and sub_total > 0:
        # 一个关键词精确命中 + 子词部分匹配
        sub_ratio = sub_matched / max(sub_total, 1)
        return 0.6 + sub_ratio * 0.3
    elif exact_matched == 1:
        return 0.6
    elif sub_total > 0:
        # 无精确命中但子词有部分匹配
        sub_ratio = sub_matched / max(sub_total, 1)
        return sub_ratio * 0.4
    return 0.0


def check_query(query: str, threshold: float = 0.5) -> Optional[dict]:
    """
    检查用户问题是否与映射表中的法律误区匹配。
    如果匹配，返回对应的纠正信息；否则返回 None。

    Args:
        query: 用户问题
        threshold: 匹配阈值（0~1），越高越严格

    Returns:
        {
            "matched": True,
            "scenario": "用户常见问法/场景",
            "law": "实际法律依据",
            "citation": "正确引用索引",
            "message": "引导用户转向正确法律依据的提示信息"
        }
        或 None（无匹配）
    """
    # 如果问题明确提到了「民法典」或具体法条编号，跳过映射表
    #    避免将明确的民法典问题误归入其他法律。
    #    口径对齐（2026-09-28 agents 链审查 LM-R1）：裸"第X条"**不足以**判定民事——
    #    routing_table 2026-09-06 就修过同款坑（"劳动合同法第39条"借裸条号绕过
    #    黑名单），此处旧写法 `第[0-9零一…]+条` 让同一样例在映射表层再次旁路：
    #    他法带条号的问题跳过纠正引导，直接流进民法典检索，召回不相关条文。
    #    现要求"民法典/民法通则/民法总则"前缀紧跟条号才跳过。
    if "民法典" in query or re.search(
            r'(民法典|民法通则|民法总则)\s*第[一二三四五六七八九十百千零0-9]+[条编款]',
            query):
        return None

    entries = _load_mapping()
    if not entries:
        return None

    best_match = None
    best_score = 0.0

    for entry in entries:
        score = _calculate_match_score(query, entry["keywords"])
        if score > best_score:
            best_score = score
            best_match = entry

    if best_match and best_score >= threshold:
        law = best_match["law"]
        citation = best_match["citation"]
        return {
            "matched": True,
            "id": best_match["id"],
            "scenario": best_match["scenario"],
            "law": law,
            "citation": citation,
            "wrong_area": best_match["wrong_area"],
            "match_score": round(best_score, 2),
            "message": (
                f"您好，我是民法典助手！您的问题属于{law}"
                f"（{citation}）调整的范畴，请咨询相关领域的法律专业人士或行政机关以获取权威解答。"
            ),
        }

    return None


def get_all_entries() -> list[dict]:
    """获取所有映射条目（不含message）"""
    return _load_mapping()


def reload_mapping():
    """重新加载映射表（用于热更新；锁保护防并发读到半清空状态，P1 #12）

    锁为 RLock：本函数持锁后再调 `_load_mapping`，同线程二次进入同一把锁（P1-4）。
    """
    global _LAW_MAPPING_CACHE
    with _LOAD_LOCK:
        _LAW_MAPPING_CACHE = None
        return _load_mapping()

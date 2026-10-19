"""
意图词表模块 — 关键词到领域的映射 + 语言无关的命中判定

拆出缘由（2026-10-18）：router.py 因接英文意图从 575 行涨到 611 行，越过
「单文件 ≤600 行」硬限。按子系统边界搬迁——本模块只装"词表 + 怎么算命中"，
分发决策（classify_intent / handle_simple_task）仍留在 router.py。

组成:
  1. INTENT_KEYWORDS - 领域 → 关键词表（中英混排）
  2. RECOMMEND_PATTERNS - 全推荐（行程规划）意图短语
  3. SIMPLE_MAX_DOMAINS - 命中领域数超过它即判复杂任务
  4. match_terms() - 中文按子串、英文按词边界的统一命中判定

改词表不动 router，改分发逻辑不动本模块；两边都只有一把尺。
"""

import re
from functools import lru_cache
from typing import Dict, List, Tuple

# ============================================================
# 关键词意图映射表
# ============================================================
INTENT_KEYWORDS: Dict[str, List[str]] = {
    "query_weather": ["天气", "气温", "温度", "下雨", "晴天", "刮风", "气候", "气象",
                      "天气预报", "几度", "多少度", "冷不冷", "热不热",
                      "weather", "temperature", "forecast", "rainy", "raining",
                      "sunny", "climate", "windy", "how hot", "how cold", "degrees"],
    "query_hotel":   ["酒店", "住宿", "旅馆", "民宿", "客房", "房间", "留宿", "过夜",
                      "订房", "预约酒店", "宾馆", "入住",
                      "hotel", "accommodation", "lodging", "resort", "guesthouse",
                      "where to stay", "book a room", "place to stay"],
    "query_route":   ["路线", "怎么去", "交通", "乘车", "驾车", "公交", "地铁", "到达", "前往",
                      "开车", "打车", "坐车", "多远", "多久",
                      "坐什么车", "几路", "导航", "怎么走",
                      "route", "directions", "transportation", "transport",
                      "how to get", "get there", "flight", "train", "subway",
                      "metro", "taxi", "driving", "how far", "by bus", "bus station"],
    "query_food":    ["美食", "餐厅", "吃饭", "菜系", "小吃", "特色菜", "餐饮",
                      "馆子", "饭馆", "食堂", "好吃", "推荐菜", "吃货", "去哪吃",
                      "food", "restaurant", "cuisine", "dining", "local dish",
                      "specialty", "snack", "what to eat", "where to eat", "eating"],
    "search_knowledge": ["民法典", "法律", "法条", "法规", "合同法", "侵权", "继承",
                         "婚姻", "物权", "债权", "诉讼时效", "民事",
                         "权利", "义务", "赔偿", "纠纷", "打官司",
                         "civil code", "legal advice", "lawyer", "lawsuit",
                         "contract law", "inheritance law", "marriage law"],
    "web_search":    [],  # 由 LLM 自行判断是否调用，不通过关键词匹配
}

# 如果匹配"推荐"涵盖四个领域，则为全推荐模式
RECOMMEND_PATTERNS = [
    "推荐", "建议", "攻略", "安排", "规划行程", "旅游计划",
    "有什么好玩", "帮我规划", "行程安排", "旅行攻略",
    "重新规划", "调整行程", "修改行程", "改行程",
    "改目的地", "改出发地", "目的地改成", "出发地改成",
    # 英文旅行规划意图（用短语避免裸 plan/trip 误伤 plane/script）
    "plan my trip", "plan a trip", "plan the trip", "plan my travel",
    "plan my vacation", "travel plan", "trip plan", "travel itinerary",
    "itinerary", "things to do", "what to do", "travel guide", "trip guide",
    "recommend", "suggestion", "vacation", "holiday", "tour", "travel",
]

# 法律场景标记（P1 #77）：命中即视为法律咨询，"推荐/建议"不触发旅游全推荐。
# 中文四词 + search_knowledge 域已有的英文法律词——10-15 起 RECOMMEND_PATTERNS
# 含英文 recommend/suggestion，若守卫只认中文，英文法律句会挂上旅游四域。
LEGAL_SCENE_MARKERS = (
    "民法典", "法律", "法条", "法规",
    "civil code", "legal advice", "lawyer", "lawsuit",
    "contract law", "inheritance law", "marriage law",
)

# 如果匹配到多个领域的关键词，超过此数量视为复杂任务
SIMPLE_MAX_DOMAINS = 1


@lru_cache(maxsize=None)
def _ascii_word_re(terms: Tuple[str, ...]):
    """把英文关键词预编译成词边界正则：非字母数字前后视，防 rain⊂train、eat⊂great 误命中。"""
    parts = [re.escape(t.lower()) for t in terms if t.isascii()]
    if not parts:
        return None
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(parts) + r")(?![a-z0-9])")


def match_terms(query: str, terms) -> bool:
    """语言无关的关键词命中：中文按子串、英文按词边界（大小写不敏感），命中任一即 True。"""
    q = query.lower()
    if any(t and not t.isascii() and t in q for t in terms):
        return True
    rx = _ascii_word_re(tuple(t for t in terms if t.isascii()))
    return bool(rx and rx.search(q))

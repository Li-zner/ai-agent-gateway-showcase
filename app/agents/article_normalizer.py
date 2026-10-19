"""
法条编号归一化工具
将口语化的"第X条"自动映射为"第一千零X条"格式
"""
import re

# 数字映射表
_DIGIT_MAP = {
    '0': '零', '1': '一', '2': '二', '3': '三', '4': '四',
    '5': '五', '6': '六', '7': '七', '8': '八', '9': '九',
}

# 中文数字→阿拉伯数字映射。「两」是口语写法（"两"＝"二"）：条号字符类放开两的同时
# 这里必须有值，否则 _chinese_to_arabic 把它当分隔符算出 0，会被
# normalize_article_ref 的 `num <= 0` 分支静默旁路——只改正则等于没改。
_CN_DIGIT_MAP = {
    '零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4,
    '五': 5, '六': 6, '七': 7, '八': 8, '九': 9,
    '十': 10, '百': 100, '千': 1000,
}

# 法条编号前缀映射（民法典编目）
_BOOK_PREFIX = {
    1: '第一编 总则',
    2: '第二编 物权',
    3: '第三编 合同',
    4: '第四编 人格权',
    5: '第五编 婚姻家庭',
    6: '第六编 继承',
    7: '第七编 侵权责任',
}


def _arabic_to_chinese(num: int) -> str:
    """阿拉伯数字转中文数字（支持1-9999），正确处理零位"""
    if num == 0:
        return "零"  # P2 #16：显式处理 0，避免返回空字符串
    if num < 10:
        return _DIGIT_MAP.get(str(num), str(num))
    
    digits = str(num)
    length = len(digits)
    
    if length == 4:
        # 千位
        result = _DIGIT_MAP[digits[0]] + '千'
        remaining = int(digits[1:])
        if remaining == 0:
            return result
        if remaining < 100:
            result += '零'
        result += _arabic_to_chinese(remaining)
        return result
    
    if length == 3:
        result = _DIGIT_MAP[digits[0]] + '百'
        remaining = int(digits[1:])
        if remaining == 0:
            return result
        if remaining < 10:
            result += '零'
        result += _arabic_to_chinese(remaining)
        return result
    
    if length == 2:
        if digits[0] == '1':
            result = '十'
        else:
            result = _DIGIT_MAP[digits[0]] + '十'
        remaining = int(digits[1])
        if remaining > 0:
            result += _DIGIT_MAP[digits[1]]
        return result
    
    return _DIGIT_MAP.get(digits[0], digits[0])


def _chinese_to_arabic(text: str) -> int:
    """中文数字转阿拉伯数字"""
    total = 0
    current = 0
    for char in text:
        if char in _CN_DIGIT_MAP:
            val = _CN_DIGIT_MAP[char]
            if val >= 10:
                if current == 0:
                    current = 1
                total += current * val
                current = 0
            else:
                current = val if current == 0 else current * 10 + val
        else:
            if current > 0:
                total += current
                current = 0
    total += current
    return total


# 正则：匹配"第X条"、"第X条之一"等格式；左分支是范围写法"第X-Y条"（含 -~至到），
# 右分支是单条。2026-09-18 新增范围分支：此前正则整体不认"第394-404条"，
# 归一化对范围查询完全旁路，trgm 词面路与 _promote_pinned 条号腿都看不见中文条号。
# 2026-10-13 字符类补「零」「两」：用户直接写中文"第一千零七十七条"时整条匹配不上，
# 归一化与条号提取双双旁路。字符类与行首条号那三处同一把尺，由
# test_article_head_charset_parity_with_retrieval_side 扫描源码钉住。
_ARTICLE_PATTERN = re.compile(
    r'第(?P<from>\d{1,4})\s*[-~～–—至到]\s*(?P<to>\d{1,4})条'
    r'|第(?P<num>[一二三四五六七八九十百千零两0-9]+)条(?:之一|之二|之三|之四)?')

# 范围展开上限：超过则不改写。逐条展开会等比拉长检索文本（trgm 查询与向量
# embedding 输入共用），几十上百条时词面稀释已超过点名收益。
_RANGE_MAX = 30

# 其它法律名称：紧跟"第X条"时说明引用的不是民法典条文，不做归一化
_OTHER_LAW_NAMES = (
    "消费者权益保护法", "食品安全法", "劳动法", "劳动合同法", "刑法",
    "行政处罚法", "治安管理处罚法", "公司法", "商标法", "专利法", "著作权法",
    "环境保护法", "税收征收管理法", "道路交通安全法", "保险法", "个人所得税法",
)

# 法律名与「第X条」之间可能夹的标点/空白：书名号、引号、全半角空格、制表符。
# 判"某法律名是否紧挨在条号前面"时先剥掉它们，否则《》一夹就断（2026-10-07 P1-2）。
_LAW_NAME_SEPARATORS = "《》「」『』“”\"'’ ‘ \t\r\n　"


def normalize_article_ref(query: str) -> str:
    """
    将口语化的法条引用归一化。
    
    例如：
    "第1077条" → "民法典第一千零七十七条"
    "第1079条" → "民法典第一千零七十九条"
    "第366条"  → "民法典第三百六十六条"

    Args:
        query: 用户查询文本

    Returns:
        归一化后的文本（未匹配则返回原文）
    """
    def _other_law_ahead(start: int) -> bool:
        """引用前紧挨着其它法律名 → 那是那部法律的条文，整体不改写（否则会拼出"消保法民法典第X条"）

        剥尾部书名号/空白再判（2026-10-07 C1/P1-2）：写「《消费者权益保护法》第四十九条」时
        法律名与条号之间夹着 》，旧写法要求零间隔相等，他法保护整个失效。
        """
        prev = query[:start].rstrip(_LAW_NAME_SEPARATORS)
        return any(prev.endswith(p) for p in _OTHER_LAW_NAMES)

    def _head(start: int) -> str:
        """该引用是否需注入「民法典」前缀（原文已带民法/民法典前缀则不注入）

        与 _other_law_ahead 同一个剥壳口径（2026-10-07 C1/P1-2）：「《民法典》第366条」
        的前缀隔着 》，旧写法逐字相等判定失效，会在书名号内再拼一个「民法典」。
        """
        prev = query[:start].rstrip(_LAW_NAME_SEPARATORS)
        if prev.endswith("民法典") or prev.endswith("民法"):
            return ""
        return "民法典"

    def _replace(match):
        if _other_law_ahead(match.start()):
            return match.group(0)
        head = _head(match.start())
        if match.group("from"):
            # 范围写法：逐条展开为中文数字条号列表，让 trgm 词面与 pin 提升可见。
            # re.sub 不回扫替换文本，展开产物不会被单条分支二次注入前缀。
            start_num, end_num = int(match.group("from")), int(match.group("to"))
            if (not 1 <= start_num <= end_num <= 1260
                    or end_num - start_num + 1 > _RANGE_MAX):
                return match.group(0)
            arts = "、".join(f"第{_arabic_to_chinese(n)}条"
                             for n in range(start_num, end_num + 1))
            return head + arts
        num_str = match.group("num")
        if num_str.isdigit():
            num = int(num_str)
        else:
            num = _chinese_to_arabic(num_str)

        if num <= 0:
            return match.group(0)

        # 民法典法条范围（1-1260条）；本应用是民法典助手，范围内的编号一律归入民法典。
        # 超出范围（其它法律的条文编号，如消保法共63条也在范围内被民法典优先覆盖）不归一化。
        # 注意：消保法条文号与民法典完全重叠，无上下文无法区分，按民法典处理是有意为之
        if 1 <= num <= 1260:
            return head + f"第{_arabic_to_chinese(num)}条"
        return match.group(0)
    
    return _ARTICLE_PATTERN.sub(_replace, query)


def get_article_number(query: str) -> int | None:
    """从查询中提取法条编号（范围写法取首条号）"""
    match = _ARTICLE_PATTERN.search(query)
    if not match:
        return None
    if match.group("from"):
        return int(match.group("from"))
    num_str = match.group("num")
    if num_str.isdigit():
        return int(num_str)
    return _chinese_to_arabic(num_str)

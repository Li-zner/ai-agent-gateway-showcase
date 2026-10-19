"""出发地/目的地提取（纯函数、仅依赖 constants，可独立单测）

2026-09 修复"广州/成都"泄漏 bug 的公共层：此前三处各自实现的工具参数提取
（stream_utils._auto_args / chat_fast_paths.build_simple_tool_args /
router._extract_simple_args）存在同一组语义错误：
  - 侧栏"出发地"user_location 被当成 hotel/food 的目的地（用户填广州，问成都，查的却是广州）
  - weather 按城市表顺序扫描，"从广州去成都"先命中表中靠前的广州
  - route 的 destination 被塞入整句用户原话，子代理拿到的参数是垃圾
模型拿到自相矛盾的工具数据后把内部推理整段漏进正文。

语义（2026-09 定稿，出发地手动输入功能已整体移除）：
  - 出发地 = 问题中显式"从X"；route 无显式出发地时由子代理按"当前位置"处理
  - 目的地 = 问题中"去/到/前往/飞X"的城市 > 其他城市候选 > 原话兜底
"""
import hashlib
import json
import re
from .constants import CITIES

# 口语别名归一（原 stream_utils._CITY_ALIASES 同源）
CITY_ALIASES = {
    "帝都": "北京", "魔都": "上海", "羊城": "广州", "鹏城": "深圳",
    "蓉城": "成都", "山城": "重庆", "春城": "昆明", "冰城": "哈尔滨",
    "泉城": "济南", "榕城": "福州", "星城": "长沙", "江城": "武汉",
}
# 别名归一后的城市集合：_plausible_name 的"命中城市表"判据把别名一并算进来
_ALIASED_CITIES = frozenset(CITY_ALIASES.values())

# 目的地标志词。不含"在"——"我在广州"是位置陈述而非目的地意图
_DEST_MARKERS = ("去", "到", "前往", "飞")

# "从A到B"整句路线（支持城市表之外的地名，如"从天河到机场"）
_FROM_TO_RE = re.compile(r"从(.{2,12}?)到(.{2,12}?)(?:[，。,.\s]|怎么|的|$)")

# 城市表长尾兜底：表只维护热门城市，"想去日照旅游""从梧州市出发"这类表外地名
# 靠标志词 + 边界正则提取（2026-09-10 服务器实测：提取空串导致整句被当 city 传给
# 高德 geocode 必然查不到）。捕获含疑问/指代字的不算地名（"去哪里玩""去哪都行"）；
# 高德对真实不存在的名字自行返回"未找到"，误提取代价可控
_NONLIST_DEP_RE = re.compile(r"从([\u4e00-\u9fa5]{2,6}?)(?:市)?出发")
_EXPLICIT_DEP_RE = re.compile(
    r"(?:出发地|出发城市|起点)\s*"
    r"(?:改成|改为|调整为|变成|换成|设为|是|为|到)?\s*"
    r"([\u4e00-\u9fa5]{2,8}?)(?=出发|旅游|旅行|游玩|玩|[,，。；;\s]|$)"
)
_NONLIST_DEST_RE = re.compile(
    r"(?:想去|想玩|前往|去|到|飞)([\u4e00-\u9fa5]{2,6}?)(?:市)?"
    r"(?=旅游|旅行|游玩|玩一玩|玩|[,，。.！！？?\s]|$)"
)
_EXPLICIT_DEST_RE = re.compile(
    r"(?:目的地|目标城市|终点)\s*"
    r"(?:改成|改为|调整为|变成|换成|设为|是|为|到)?\s*"
    r"([\u4e00-\u9fa5]{2,8}?)(?=旅游|旅行|游玩|玩|[,，。；;\s]|$)"
)
# "出发地改成日照"是明确赋值，"出发地不知道"是拒答——前者必须放行表外城市，
# 后者交给 _plausible_name 的拒答词表拦（2026-09-22 审阅 P1）。只取无歧义的
# 改值动词，"是/为/到"太常用在疑问与铺陈里，不算赋值凭据。
_ASSERT_CHANGE_RE = re.compile(r"(?:改成|改为|调整为|变成|换成|设为)")
# "渭南旅游/梧州旅行"没有"去/到"标志词，但仍是明确的旅游目的地。
_BARE_TOUR_DEST_RE = re.compile(
    r"([\u4e00-\u9fa5]{2,6}?)(?:市)?(?:旅游|旅行|游玩)"
)
# 英文标志词 + 紧邻中文地名：EN/中文混查场景（句子是英文、城市名是中文，如
# "traveling from 梧州市 to 盐城"）。此前所有目的地正则都要求中文标志词
# （去/到/前往/飞）紧邻地名，英文句式没有这些词、城市又常在 68 城表外，于是
# extract_destination 返回空——2026-10-15 活体复现：梧州→盐城英文查询被
# recommend 路径反问"想去哪个城市"。只抓 CJK 串、标志词前设字母边界、标志词
# 后须跟空白，纯中文句不会命中（无 " to " 空格边界）；可信度仍过 _plausible_name。
# ponytail: "to 大家/我们"这类中文代词仍可能误判，彻底收口交 tools 层 geocode 校验。
_EN_DEST_RE = re.compile(
    r"(?<![A-Za-z])(?:to|toward|towards|visiting|visit|flying to|going to"
    r"|travel(?:ing)? to|head(?:ing)? to|arrived? in)\s+"
    r"([\u4e00-\u9fa5]{2,8}?)(?:市)?(?=[\s,，.。!！?？;；]|$)"
)
_EN_DEP_RE = re.compile(
    r"(?<![A-Za-z])(?:from|leaving from|departing from|flying from)\s+"
    r"([\u4e00-\u9fa5]{2,8}?)(?:市)?(?=[\s,，.。!！?？;；]|$)"
)
# 旅游词前的活动/范围词不是地名，避免"国内旅游/亲子旅游"被当城市。
_NON_PLACE_TRAVEL_WORDS = {
    "国内", "周边", "亲子", "情侣", "自由", "自由行", "当地", "境外",
    "出国", "自驾", "毕业", "周末", "寒假", "暑假",
}
_FUNC_CHAR_RE = re.compile(r"[哪这怎几吗呢啥]")

# 待补槽场景下，用户常用"从梧州出发/广州吧"回复；这里只归一化地点本体。
_PLACE_REPLY_PREFIX_RE = re.compile(
    r"^(?:从|我在|出发地(?:是|为)?[:：]?|就|选|我选|当然是|是)\s*"
)
_PLACE_REPLY_SUFFIX_RE = re.compile(
    r"\s*(?:出发|吧|呀|啊|哦|了|的|就行|可以|都可以)\s*$"
)
_PLACE_REPLY_REJECT = {
    "算了", "不用", "取消", "不知道", "不晓得", "随便", "都行", "你好",
    "您好", "什么", "哪里", "哪儿", "为啥", "为什么", "没有", "不去",
    "先不", "晚点", "再说",
}

# 旅游缓存约束指纹的归一化词表。这里只保留会改变答案的维度，
# 不把“攻略/推荐”等通用词纳入，避免无意义地碎片化缓存。
_TRAVEL_MARKERS = (
    "旅游", "旅行", "游玩", "旅游计划", "旅游攻略", "行程", "路线",
    "景点", "度假", "自由行", "亲子游", "情侣游", "一日游", "两日游",
    "三日游", "四日游", "五日游", "酒店", "住宿", "美食", "小吃",
)
_TRAVEL_DATE_TERMS = {
    "今天": "今天", "明天": "明天", "后天": "后天", "周末": "周末",
    "工作日": "工作日", "国庆": "国庆", "五一": "五一", "春节": "春节",
    "寒假": "寒假", "暑假": "暑假",
    "春天": "春天", "春季": "春天", "夏天": "夏天", "夏季": "夏天",
    "秋天": "秋天", "秋季": "秋天", "冬天": "冬天", "冬季": "冬天",
}
_TRAVEL_COMPANION_TERMS = {
    "亲子": "亲子", "带孩子": "亲子", "带娃": "亲子", "孩子": "亲子",
    "小孩": "亲子", "儿童": "亲子", "宝宝": "亲子",
    "父母": "父母", "老人": "老人", "长辈": "老人",
    "情侣": "情侣", "夫妻": "情侣", "爱人": "情侣",
    "男朋友": "情侣", "女朋友": "情侣",
    "朋友": "朋友", "同学": "朋友", "同事": "朋友",
    "一个人": "单人", "独自": "单人", "单人": "单人",
    "家庭": "家庭", "一家": "家庭",
}
_TRAVEL_BUDGET_LEVELS = {
    "经济": "经济", "省钱": "经济", "便宜": "经济", "实惠": "经济",
    "中等": "中等", "适中": "中等", "舒适": "舒适", "豪华": "豪华",
    "高端": "豪华",
}
_TRAVEL_THEME_TERMS = {
    "景点": "景点", "游玩": "游玩", "路线": "路线", "行程": "行程",
    "酒店": "住宿", "民宿": "住宿", "住宿": "住宿", "住宿区域": "住宿",
    "美食": "美食", "小吃": "美食", "餐厅": "美食", "当地菜": "美食",
    "海鲜": "海鲜", "烧烤": "烧烤", "火锅": "火锅",
    "亲子": "亲子", "情侣": "情侣", "老人": "老人",
    "海边": "海边", "市区": "市区", "周边": "周边",
    "户外": "户外", "爬山": "户外", "博物馆": "博物馆",
    "拍照": "拍照", "休闲": "休闲", "轻松": "轻松", "紧凑": "紧凑",
    "安静": "安静", "热闹": "热闹", "小众": "小众", "热门": "热门",
    "历史": "历史", "免费": "免费", "自然": "自然", "购物": "购物",
    "故宫": "故宫", "天安门": "天安门", "长城": "长城", "颐和园": "颐和园",
    "西湖": "西湖", "灵隐寺": "灵隐寺", "西溪湿地": "西溪湿地",
    "断桥": "断桥", "河坊街": "河坊街", "迪士尼": "迪士尼",
    "自然博物馆": "自然博物馆", "栈桥": "栈桥", "八大关": "八大关",
    "崂山": "崂山", "宽窄巷子": "宽窄巷子", "春熙路": "春熙路",
}
_TRAVEL_DAY_RE = re.compile(
    r"(?<!第)(?<!月)([零一二两三四五六七八九十百\d]{1,3})\s*(?:天|日)(?:游)?"
)
_TRAVEL_AGE_RE = re.compile(r"([零一二两三四五六七八九十百\d]{1,3})\s*岁")
_TRAVEL_MONEY_RE = re.compile(r"([零一二两三四五六七八九十百千万\d]{1,8})\s*(?:元|块)")
_TRAVEL_BUDGET_RE = re.compile(
    r"预算(?:控制在|在|大约|约|大概|是|为|[:：])?\s*"
    r"([零一二两三四五六七八九十百千万\d]{1,8})"
)
_TRAVEL_DATE_RE = re.compile(r"\d{1,2}月(?:\d{1,2}[日号])?")
_TRAVEL_PEOPLE_COUNT_RE = re.compile(
    r"([零一二两三四五六七八九十\d]{1,3})\s*个?人"
)
_TRAVEL_FAMILY_COUNT_RE = re.compile(
    r"一家\s*([零一二两三四五六七八九十\d]{1,3})\s*口"
)
_TRAVEL_ADULT_CHILD_RE = re.compile(
    r"([零一二两三四五六七八九十\d]{1,3})\s*大\s*"
    r"([零一二两三四五六七八九十\d]{1,3})\s*小"
)
_CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}


def _number_to_int(text: str) -> int:
    """把阿拉伯数字或常见中文数字转成整数，供天数/年龄/预算归一化。"""
    if text.isdigit():
        return int(text)
    total = section = number = 0
    for char in text:
        if char in _CN_DIGITS:
            number = _CN_DIGITS[char]
        elif char in _CN_UNITS:
            unit = _CN_UNITS[char]
            section += (number or 1) * unit
            number = 0
        elif char == "万":
            total += (section + number) * 10000
            section = number = 0
    return total + section + number


def _matched_terms(query: str, mapping: dict) -> list:
    """按词表提取并归一化约束字段，去重后排序以保证指纹稳定。"""
    return sorted({value for key, value in mapping.items() if key in query})


# 天气问句功能词：剥掉后剩余部分若像地名则直接作为城市（"梧州天气"→梧州）。
# 2026-09-10 线上案例："天气怎么样"经原话兜底整句传给高德 geocode 必然失败，
# 模型只能凭通用知识瞎答（连日期都说错）。
_WEATHER_JUNK_RE = re.compile(
    r"天气|气温|温度|降雨|下雨|下雪|风力|风向|怎么样|怎样|如何|什么情况|"
    r"今天|明天|后天|昨天|现在|当前|这周|周末|查一下|查询|查查|看看|一下|"
    r"请问|告诉我|的|吗|呢|吧|？|\?|。|，|,|、|\s|"
    r"我|你|需要|想要|知道|具体|详细|准确|大概"
)
# 剥完功能词后残端的首尾语气/连写字（"梧州会下雨吗"→"梧州会"→梧州）。
# 刻意不含首部的"都/会/不"：都江堰、会理、不丹是真地名，只能从尾端修剪。
_WEATHER_EDGE_JUNK_RE = re.compile(r"^[很挺还再又才刚]+|[会的吗呢吧不很挺还再又才刚]+$")


def default_city() -> str:
    """无任何地点语义时的兜底城市（DEFAULT_CITY 环境变量可配）。

    仅 weather 路径的最后一级兜底（会话上下文里也找不到城市时才用）；
    hotel/food/route 仍走原话兜底交给子代理语义解析，不在此收口。
    """
    from .config import DEFAULT_CITY
    return DEFAULT_CITY


def geocode_contains(candidate: str, geocode: dict) -> bool:
    """geocode 包含性守卫（weather 与路线规划共用，单一真相源）。

    高德对垃圾输入也会模糊匹配（线上实测"我需要具体"被匹配到枣庄薛城区，
    2026-09-10 weather 事故）：要求候选词出现在匹配结果的 city/district/
    省/地址串里才认账，否则视为未找到。原先只 weather 有此守卫，路线规划的
    geocode 漏同款（"从我家到你家"会被规划成两个随机城市）——两处定义
    漂移第 12 例，2026-09-29 下沉共享。
    """
    if not candidate:
        return False
    matched = "".join(str(geocode.get(k) or "") for k in
                      ("city", "district", "province", "formatted_address"))
    return candidate in matched


def extract_context_city(history: list | None) -> str:
    """从会话历史里找最近被提及的城市（2026-09-10 主人定夺的语义优先级：
    问"天气怎么样"没提地点时，应查上下文目的地，而非默认主人地址）。

    只扫 user 消息且新消息优先——assistant 的推荐列表可能一口气列十个城市，
    扫它会张冠李戴。找不到返回空串。
    """
    if not history:
        return ""
    for msg in reversed(history):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        city = extract_destination(str(msg.get("content", "")))
        if city:
            return city
    return ""


def extract_weather_city(user_query: str, history: list | None = None) -> str:
    """weather 专用城市提取：当前句目的地 > 剥天气功能词后的短地名
    > 会话历史最近提及的城市 > 空串（默认城市由调用方最后兜底）。

    不再以原话兜底——整句话当 city 传高德是必败路径（geocode 查不到句子）。
    ponytail: 剥词表是启发式，覆盖常见问法即可；残端漏网（"我需要具体"曾剥出
    候选被高德模糊匹配到枣庄薛城区）由 tools 层的 geocode 包含性守卫兜底，
    升级路径是接入地名实体识别。
    """
    city = extract_destination(user_query)
    if city:
        return city
    stripped = _WEATHER_EDGE_JUNK_RE.sub("", _WEATHER_JUNK_RE.sub("", user_query))
    if _plausible_name(stripped) and 2 <= len(stripped) <= 6:
        return strip_shi(stripped)
    return extract_context_city(history)


def _plausible_name(name: str, require_listed: bool = False,
                    asserted: bool = False) -> bool:
    """地名候选可信度：非空、不含疑问/指代字、且不是"不知道/随便"这类拒绝语。

    2026-09-22 审阅 P1：显式出发地正则把拒绝语当成了城市——"出发地不知道"提取出
    origin="不知道"，再经 extract_profile_updates→_update_profile_async 写进
    **全局画像**，此后每一轮都带着这个假出发地。拒绝语集与短回复补槽共用同一份
    （_PLACE_REPLY_REJECT），避免两处各自漂移。

    require_listed=True 时还要求命中城市表/别名（或自带行政后缀）：只用于
    "出发地X/目的地X"这类显式句式——它的值直接进画像并作为 route 的 departure
    传高德，漏了有"当前位置"兜底。
    asserted=True（句中有"改成/换成"这类改值动词）时放弃这一层：用户是在明确
    赋值，而城市表只有 68 城，日照/梧州都不在表内（2026-09-10 线上实测），
    一刀切会把真地名一起砍掉、并让画像继续留着旧出发地。
    ponytail: 改值动词后的表外候选仍可能吃掉"出发地改成没想好"这类短语，
    彻底收口要在写入侧过一次 geocode 校验（属 tools/conversation_profiles 范围）。
    """
    if not name or _FUNC_CHAR_RE.search(name) or name in _PLACE_REPLY_REJECT:
        return False
    if not require_listed or asserted:
        return True
    bare = strip_shi(name)
    return bare in CITIES or bare in _ALIASED_CITIES or name.endswith(("市", "县"))


_TOUR_LEAD_MARKER_RE = re.compile(r"^[想去到飞往在从来]")


def _plausible_tour_dest(candidate: str) -> bool:
    """旅游目的地候选的共用守卫（2026-09-22 审阅 P1）：非拒绝语、非活动/范围词。

    此前黑名单 _NON_PLACE_TRAVEL_WORDS 只挂 _BARE_TOUR_DEST_RE 那一轮，
    上一行的 _NONLIST_DEST_RE 没过守卫——"去周边玩""想到国内旅游"带标志词，
    正好被它抓成目的地；两个循环共用后同类问题只剩这一处判据。
    判据本身按子串而非等值：裸旅游正则会把标志词一起吞进候选
    （"想到国内旅游" → "想到国内"），等值比较对这种整段无能为力。
    """
    name = strip_shi(candidate)
    if not _plausible_name(name) or _TOUR_LEAD_MARKER_RE.match(name):
        return False
    return not any(word in name for word in _NON_PLACE_TRAVEL_WORDS)


def strip_shi(name: str) -> str:
    """剥"市"后缀（高德等接口与展示都用裸城市名）"""
    return (name or "").replace("市", "")


def query_cities(user_query: str) -> list:
    """问题中出现的城市候选（别名归一后保序去重）"""
    found: list = []
    for alias, city in CITY_ALIASES.items():
        if alias in user_query and city not in found:
            found.append(city)
    for c in CITIES:
        if c in user_query and c not in found:
            found.append(c)
    return found


def extract_departure(user_query: str) -> str:
    """出发地：仅认问题中显式"从X"（无定位输入，不再兜底）

    城市表未收录的地名走"从X市出发"正则兜底。显式"出发地X"分支额外过
    _plausible_name：本函数的返回值会进全局画像，拒答语一旦被认成城市，
    此后每一轮都带着它（2026-09-22 审阅 P1）。
    """
    m = _EXPLICIT_DEP_RE.search(user_query)
    if m and _plausible_name(
            m.group(1), require_listed=True,
            asserted=bool(_ASSERT_CHANGE_RE.search(user_query))):
        return strip_shi(m.group(1))
    # 英文出发标志词 + 中文地名（"from 梧州市"）：中文"从{c}"表扫描对英文句式
    # 无从命中，这一层专补 EN/中文混查的出发地。
    m = _EN_DEP_RE.search(user_query)
    if m and _plausible_name(strip_shi(m.group(1))):
        return strip_shi(m.group(1))
    for c in query_cities(user_query):
        if f"从{c}" in user_query:
            return c
    m = _NONLIST_DEP_RE.search(user_query)
    if m and _plausible_name(m.group(1)):
        return strip_shi(m.group(1))
    return ""


def extract_destination(user_query: str) -> str:
    """目的地：标志词城市（跳过被"从"标记的出发地）> 其他候选 > 唯一候选兜底
    > 表外地名正则兜底 > 空串

    最后一级回退到唯一候选，覆盖"先查广州天气"这类唯一城市恰为出发地的问法。
    """
    m = _EXPLICIT_DEST_RE.search(user_query)
    if m and _plausible_name(
            m.group(1), require_listed=True,
            asserted=bool(_ASSERT_CHANGE_RE.search(user_query))):
        return strip_shi(m.group(1))
    # 英文目的标志词 + 中文地名（"to 盐城"/"visit 盐城"）：先于表扫描——既补 68
    # 城表外的长尾城市，也避免"from 广州 to 盐城"里表内出发地广州被唯一候选兜底
    # 当成目的地（英文句式没有中文"去/到"标志词，后续 marker 循环同样无从命中）。
    dep_en = extract_departure(user_query)
    for m in _EN_DEST_RE.finditer(user_query):
        cand = strip_shi(m.group(1))
        if cand != dep_en and _plausible_name(cand):
            return cand
    cands = query_cities(user_query)
    dep = extract_departure(user_query)
    for c in cands:
        if c == dep and f"从{c}" in user_query:
            continue
        if any(m + c in user_query for m in _DEST_MARKERS):
            return c
    for c in cands:
        if c != dep:
            return c
    if cands:
        return cands[0]
    # 城市表长尾："想去日照旅游"这类表外地名，按"想去X旅游"等边界提取。
    # 两轮共用 _plausible_tour_dest：黑名单此前只护后一轮，带标志词的前一轮
    # 一句"去周边玩"就把范围词当成了城市（2026-09-22 审阅 P1）。
    for m in _NONLIST_DEST_RE.finditer(user_query):
        if _plausible_tour_dest(m.group(1)):
            return strip_shi(m.group(1))
    for m in _BARE_TOUR_DEST_RE.finditer(user_query):
        if _plausible_tour_dest(m.group(1)):
            return strip_shi(m.group(1))
    return ""


def extract_place_reply(query: str) -> str:
    """从待补槽短回复中提取地点，不接受取消/寒暄/未知等非地点答复。

    ponytail: 这是覆盖常见城市短答的启发式；复杂指代/长句升级到 LLM 槽位抽取。
    """
    text = (query or "").strip().strip("。！？!?，,、 ")
    if not text or len(text) > 20:
        return ""
    text = _PLACE_REPLY_PREFIX_RE.sub("", text)
    text = _PLACE_REPLY_SUFFIX_RE.sub("", text).strip()
    candidate = strip_shi(text)
    if candidate in _PLACE_REPLY_REJECT:
        return ""
    if not (2 <= len(candidate) <= 6 and _plausible_name(candidate)):
        return ""
    return candidate


def _extract_travel_days(query: str) -> list:
    """提取规划天数，支持阿拉伯数字和常见中文数字。"""
    values = []
    for match in _TRAVEL_DAY_RE.finditer(query):
        count = _number_to_int(match.group(1))
        if count > 0:
            values.append(f"{count}天")
    return sorted(set(values))


def _extract_travel_dates(query: str) -> list:
    """提取节日、季节、周末和显式月日；只用于缓存隔离，不做日期计算。"""
    values = _matched_terms(query, _TRAVEL_DATE_TERMS)
    values.extend(_TRAVEL_DATE_RE.findall(query))
    return sorted(set(values))


def _extract_travel_people(query: str) -> list:
    """提取同行人类别，亲子、老人、情侣等差异会导致答案不可复用。"""
    return _matched_terms(query, _TRAVEL_COMPANION_TERMS)


def _extract_travel_counts(query: str) -> list:
    """提取出行人数；类别相同但人数不同也要隔离缓存。"""
    values = []
    for match in _TRAVEL_PEOPLE_COUNT_RE.finditer(query):
        count = _number_to_int(match.group(1))
        if count > 0:
            values.append(f"{count}人")
    for match in _TRAVEL_FAMILY_COUNT_RE.finditer(query):
        count = _number_to_int(match.group(1))
        if count > 0:
            values.append(f"一家{count}口")
    for match in _TRAVEL_ADULT_CHILD_RE.finditer(query):
        adults = _number_to_int(match.group(1))
        children = _number_to_int(match.group(2))
        if adults > 0 or children > 0:
            values.append(f"{adults}大{children}小")
    return sorted(set(values))


def _extract_travel_ages(query: str) -> list:
    """提取同行年龄；同是亲子游，三岁和十二岁的安排也可能完全不同。"""
    values = []
    for match in _TRAVEL_AGE_RE.finditer(query):
        age = _number_to_int(match.group(1))
        if age > 0:
            values.append(f"{age}岁")
    return sorted(set(values))


def _extract_travel_budgets(query: str) -> list:
    """提取金额和预算等级，避免不同预算复用同一套酒店/行程答案。"""
    values = _matched_terms(query, _TRAVEL_BUDGET_LEVELS)
    for match in _TRAVEL_BUDGET_RE.finditer(query):
        amount = _number_to_int(match.group(1))
        if amount > 0:
            values.append(f"{amount}元")
    for match in _TRAVEL_MONEY_RE.finditer(query):
        amount = _number_to_int(match.group(1))
        if amount > 0:
            values.append(f"{amount}元")
    return sorted(set(values))


def _extract_travel_themes(query: str) -> list:
    """提取主题和关键地点，隔离景点、美食、住宿、景点名称等语义差异。"""
    return _matched_terms(query, _TRAVEL_THEME_TERMS)


def travel_constraint_fingerprint(user_query: str, force: bool = False) -> str:
    """生成旅游约束指纹；非旅游问题返回空串，保持原缓存行为。

    指纹只包含归一化后的约束字段，不保存原句。相同约束产生相同指纹，
    城市/日期/天数/同行人/预算/主题任一变化都会产生不同指纹。
    """
    query = user_query or ""
    if not force and not any(marker in query for marker in _TRAVEL_MARKERS):
        return ""
    constraints = {
        "city": extract_destination(query),
        "from": extract_departure(query),
        "dates": _extract_travel_dates(query),
        "days": _extract_travel_days(query),
        "people": _extract_travel_people(query),
        "travelers": _extract_travel_counts(query),
        "ages": _extract_travel_ages(query),
        "budgets": _extract_travel_budgets(query),
        "themes": _extract_travel_themes(query),
    }
    constraints = {key: value for key, value in constraints.items() if value}
    if not constraints:
        return ""
    payload = json.dumps(constraints, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def auto_tool_args(name: str, user_query: str) -> dict:
    """按工具名自动补全参数（fast_paths / dispatch_tool / router 三处共用）

    仅处理旅游四工具；其余工具返回空 dict（查询类参数由调用方自带）。
    hotel/food/route 无城市可提取时以原话兜底（子代理/接口自行失败处理）；
    weather 无城市返回空串——历史上下文与默认城市由 dispatch_tool 调用方
    依次兜底（extract_weather_city 支持传 history，2026-09-10 语义定稿）。
    """
    if name == "query_weather":
        return {"city": extract_weather_city(user_query)}
    if name in ("query_hotel", "query_food"):
        return {"destination": extract_destination(user_query) or user_query}
    if name == "query_route":
        m = _FROM_TO_RE.search(user_query)
        if m:
            dep, dest = strip_shi(m.group(1)), strip_shi(m.group(2))
        else:
            dep = extract_departure(user_query)
            dest = extract_destination(user_query)
        return {
            "departure": dep or "当前位置",
            "destination": dest or user_query,
        }
    return {}

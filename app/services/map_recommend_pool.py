"""城市美食/景点共享池：原子并入与随机抽样。

2026-09-22 审查 P2 自 app/routes/map_api.py 下沉：并入要改成单条 Redis Lua
脚本（消除 GET→合并→SET 的并发覆盖），而 map_api.py 已贴 600 行门禁（585 行）。

键口径与 09-20 定稿一致：键不带日期、全城共享、每次写入续期 15 天，值是
{"foods": [...], "spots": [...]} 的 JSON 串，全员可读。
"""
import json
import random

from ..core.logging import setup_logging

logger = setup_logging()

CITY_CACHE_PREFIX = "map:city_cache:"
RECOMMEND_TTL_SECONDS = 15 * 86400  # 每次刷新生成续期 15 天
RECOMMEND_PICK = 3                  # 普通点击每类随机抽取条数
RECOMMEND_POOL_MAX = 30             # 共享池每类封顶条数（防无界增长）
POOL_KINDS = ("foods", "spots")
# 条目字符上限（Python 侧截断）：LLM 畸形/超长输出会把一条塞进 30 槽池并驻留
# 15 天，无上限即无界内存；64 字符与城市名入键口径一致
RECOMMEND_ENTRY_MAX_CHARS = 64
# Lua 侧只**丢弃**超过该字节数的历史项：64 字符 × 3 字节（CJK UTF-8）= 192，
# 留到 256。刻意不在脚本里截断——string.sub 按字节切，会把多字节字符切成半个，
# cjson.encode 随即报错、整条脚本失败
RECOMMEND_ENTRY_MAX_BYTES = 256

# 读-合并-写在同一个脚本内完成（参照 app/routes/sms_lua.py 的原子口径）：
# KEYS[1]=池键，ARGV[1]=每类封顶，ARGV[2]=TTL 秒，ARGV[3]=新条目 JSON，
# ARGV[4]=历史项字节数上限。新条目在前、按串去重、封顶截断，与改前语义等价。
RECOMMEND_POOL_MERGE_LUA = """
local max = tonumber(ARGV[1])
local pool = {}
local raw = redis.call('GET', KEYS[1])
if raw then
    local ok, decoded = pcall(cjson.decode, raw)
    if ok and type(decoded) == 'table' then pool = decoded end
end
local incoming = {}
local ok, decoded = pcall(cjson.decode, ARGV[3])
if ok and type(decoded) == 'table' then incoming = decoded end
local max_bytes = tonumber(ARGV[4])
local merged = {}
for _, kind in ipairs({'foods', 'spots'}) do
    local list, seen = {}, {}
    local sources = {}
    if type(incoming[kind]) == 'table' then sources[#sources + 1] = incoming[kind] end
    if type(pool[kind]) == 'table' then sources[#sources + 1] = pool[kind] end
    for _, src in ipairs(sources) do
        for _, v in ipairs(src) do
            if #list >= max then break end
            if type(v) == 'string' then
                local s = v:gsub('^%s*(.-)%s*$', '%1')
                if #s > 0 and #s <= max_bytes and seen[s] == nil then
                    seen[s] = true
                    list[#list + 1] = s
                end
            end
        end
    end
    merged[kind] = list
end
redis.call('SET', KEYS[1], cjson.encode(merged), 'EX', tonumber(ARGV[2]))
return 1
"""


def _pool_key(city: str) -> str:
    """共享池键名（不带日期=全城共享）；调用方传已消毒城市名。"""
    return f"{CITY_CACHE_PREFIX}recommend:{city}"


def _sanitize_entries(values) -> list[str]:
    """写前归一：转字符串、去首尾空白、按**字符**截断、批内去重、封顶池上限。

    截断放在 Python 而非 Lua：str[:n] 按码点切，多字节中文不会被切坏。
    2026-09-29 投毒 P1 纵深：命中提示词注入话术（"忽略…指令"族，复用
    stream_utils 预编译锚）的条目直接丢弃——geocode 校验已把 city 压到
    真实城市名，这里是第二道：LLM 被城市名内嵌指令诱导产出的条目不落池。
    """
    from ..core.stream_utils import _INJECTION_PATTERNS
    out: list[str] = []
    for item in values or []:
        if item is None:
            continue  # str(None) 会把字面量 "None" 变成全员可见的"菜名"
        s = str(item).strip()[:RECOMMEND_ENTRY_MAX_CHARS]
        if not s or s in out:
            continue
        if any(p.search(s) for p in _INJECTION_PATTERNS):
            continue  # 注入话术条目不入全员共享池
        out.append(s)
    return out[:RECOMMEND_POOL_MAX]


async def recommend_pool_put(city: str, result: dict) -> None:
    """把本轮 LLM 生成结果并入共享池（新条目在前、去重、封顶、续期 15 天）。

    修复口径：合并必须留在 Redis 侧单脚本里。改前是 GET→Python 合并→SET，
    两个用户同点 force=1 时后写者拿的是自己那轮的快照，会把先写者刚并入的
    条目整块盖掉——不报错、池子悄悄缩水。
    """
    from ..core.redis import get_redis
    r = await get_redis()
    payload = {kind: _sanitize_entries(result.get(kind)) for kind in POOL_KINDS}
    await r.eval(
        RECOMMEND_POOL_MERGE_LUA, 1, _pool_key(city),
        str(RECOMMEND_POOL_MAX), str(RECOMMEND_TTL_SECONDS),
        json.dumps(payload, ensure_ascii=False), str(RECOMMEND_ENTRY_MAX_BYTES),
    )


async def recommend_pick_from_pool(city: str):
    """从城市共享池随机抽 3+3；无池/数据损坏/条数不足返回 None（触发生成）。"""
    from ..core.redis import get_redis
    r = await get_redis()
    raw = await r.get(_pool_key(city))
    if raw is None:
        return None
    try:
        pool = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(pool, dict):
        return None
    picked = {}
    for kind in POOL_KINDS:
        items = pool.get(kind)
        if not (isinstance(items, list) and len(items) >= RECOMMEND_PICK):
            return None
        picked[kind] = random.sample(items, RECOMMEND_PICK)
    return picked

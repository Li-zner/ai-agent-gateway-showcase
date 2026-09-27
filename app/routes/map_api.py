from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
import httpx
import os
import json
import re
from datetime import datetime, timezone
from ..middleware.auth import get_current_user
from ..core.config import AMAP_API_KEY, HTTP_TIMEOUT_MEDIUM, MAP_API_TIMEOUT
from ..core.logging import setup_logging
from .auth import _client_ip
from .map_utils import is_reserved_ip, weather_candidates
from .map_amap import plan_amap_route
# 共享池的并入/抽样在 services（09-22 审查 P2：合并要收进单条 Lua 脚本，
# 本文件已贴 600 行门禁）；键前缀从那里导入，避免两处定义漂移
# （2026-09-28 批1 长尾 P2：迁移后本文件只还用 CITY_CACHE_PREFIX，
# RECOMMEND_PICK/POOL_MAX/TTL_SECONDS 三个零使用名字已从 import 摘除）
from ..services.map_recommend_pool import (
    CITY_CACHE_PREFIX,
    recommend_pick_from_pool as _recommend_pick_from_pool,
    recommend_pool_put as _recommend_pool_put,
)

logger = setup_logging()

class RouteRequest(BaseModel):
    """路线请求（限长：地址串会拼进 LLM prompt，防超长输入放大计费与注入面）"""
    departure: str = Field(..., min_length=1, max_length=128)
    destination: str = Field(..., min_length=1, max_length=128)

router = APIRouter(prefix="/api/map", tags=["map"])


async def _require_map_qps(current_user: dict) -> None:
    """所有高德/外部地图入口统一 QPS 门禁。"""
    from ..middleware.rate_limit import check_qps
    if not await check_qps(
        current_user["username"], current_user.get("role", "user")
    ):
        raise HTTPException(429, "请求过于频繁，请稍后再试")


# ===== 城市缓存（两类口径，2026-09-20 主人定稿）=====
# 天气（维持原口径）：点击计数按日 zset，当日 Top-N 城市写缓存；缓存键带当日日期，
#   跨日即换新键——等价于"每天清除所有城市天气缓存"，每天首点重新生成（TTL 1 天）。
# 美食/景点（改为全城共享池）：键**不带日期**，TTL 15 天，Redis 对所有用户可读；
#   存的是该城市历次 LLM 生成过的条目合集（并入去重、封顶 30 条/类）。
#   普通点击从池中随机抽 3+3 直接返回（不走 LLM、不计费）；
#   只有刷新按钮（force=1）才重新生成并并入池、续期 15 天。
# Top-N 环境变量 CITY_CACHE_TOP_N 现仅约束天气。
CITY_HIT_ZSET = "map:city_hits:{date}"
CITY_TOP_N = int(os.getenv("CITY_CACHE_TOP_N", "200"))
WEATHER_TTL_SECONDS = 86400         # 天气：每天刷新一次
# 共享池键前缀与 RECOMMEND_* 口径见 services/map_recommend_pool.py（同一处定义）
# recommend 失败兜底数据（模块级单例：handler 用同一性判断"是兜底就不写缓存"）
_DEFAULT_REC = {"foods": ["当地特色小吃", "地道家常菜", "招牌美食"],
                "spots": ["城市地标", "历史文化街区", "自然公园"]}


def _san_city(city: str) -> str:
    """城市名入键/入日志前统一消毒：去换行（防日志伪造）+ 截断 64 字符"""
    return re.sub(r"[\r\n]", "", city or "")[:64]


async def _city_cache_get(city: str, kind: str):
    from ..core.redis import get_redis
    r = await get_redis()
    day = datetime.now().strftime("%Y%m%d")
    raw = await r.get(f"{CITY_CACHE_PREFIX}{kind}:{day}:{_san_city(city)}")
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


async def _city_cache_put(city: str, kind: str, payload, force: bool = False) -> None:
    """天气专用：点击计数（按日 zset）+ 当日 Top-N（或 force 刷新）才写缓存。
    缓存键带当日日期（跨日自然失效=每天清缓存；预报标签是"今天/明天/后天"
    相对值，不带日期的缓存会跨日展示错误日期）。美食/景点已迁共享池
    （_recommend_pool_put），不再走本函数。"""
    from ..core.redis import get_redis
    r = await get_redis()
    day = datetime.now().strftime("%Y%m%d")
    member = f"{kind}:{_san_city(city)}"
    zkey = CITY_HIT_ZSET.format(date=day)
    await r.zincrby(zkey, 1, member)
    await r.expire(zkey, 172800)  # 两天，跨自然日仍能判定"当日 Top-N"
    if not force:
        rank = await r.zrevrank(zkey, member)
        if rank is None or rank >= CITY_TOP_N:
            return  # 不在当日 Top-N：不写，保持既有缓存（若有）继续服役
    await r.set(f"{CITY_CACHE_PREFIX}{kind}:{day}:{_san_city(city)}",
                json.dumps(payload, ensure_ascii=False), ex=WEATHER_TTL_SECONDS)


# ---------- 美食/景点共享池：见 services/map_recommend_pool.py ----------
# 读池与并入池都必须在 Redis 侧单脚本内原子完成，故不在路由层实现。


@router.get("/regeo")
async def reverse_geocode(lat: float, lng: float, current_user: dict = Depends(get_current_user)):
    """经纬度→城市名（高德逆地理编码）"""
    await _require_map_qps(current_user)
    amap_key = AMAP_API_KEY
    if not amap_key:
        return {"city": ""}
    try:
        url = "https://restapi.amap.com/v3/geocode/regeo"
        async with httpx.AsyncClient(timeout=MAP_API_TIMEOUT) as client:
            resp = await client.get(url, params={
                "key": amap_key,
                "location": f"{lng},{lat}",
                "radius": 1000,
                "extensions": "base"
            })
            if resp.status_code != 200:
                return {"city": ""}
            data = resp.json()
            if data.get("status") != "1":
                return {"city": ""}
            regeo = data.get("regeocode", {})
            addr_comp = regeo.get("addressComponent", {})
            # 优先返回城市名，如果城市为空则用省份名
            city = addr_comp.get("city", "") or addr_comp.get("province", "")
            # 高德可能在直辖市返回 []，此时取 province
            if isinstance(city, list):
                city = addr_comp.get("province", "")
            province = addr_comp.get("province", "")
            if isinstance(province, list):
                province = province[0] if province else city
            return {"city": city, "province": province}
    except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError):
        return {"city": ""}


@router.get("/iploc")
async def ip_location(request: Request, current_user: dict = Depends(get_current_user)):
    """IP 近似定位兜底：浏览器 geolocation 不可用时（国内 Chrome 走 Google 定位常被墙），
    用请求 IP 经高德 IP 定位接口拿城市。CF-Connecting-IP 是 Cloudflare 传的真实用户 IP。

    私有/保留 IP（如本地测试经 docker/局域网访问）或高德无法解析时返回空——
    定位未知应让前端改问出发地，而非把"局域网"当真实城市。
    """
    await _require_map_qps(current_user)
    amap_key = AMAP_API_KEY
    if not amap_key:
        return {"city": "", "province": ""}
    # 与登录/OAuth/短信统一走同一可信代理解析，避免各入口各信一个头。
    ip = _client_ip(request)
    if not ip or is_reserved_ip(ip):
        return {"city": "", "province": ""}
    try:
        async with httpx.AsyncClient(timeout=MAP_API_TIMEOUT) as client:
            resp = await client.get("https://restapi.amap.com/v3/ip", params={"key": amap_key, "ip": ip})
            data = resp.json()
            if data.get("status") == "1":
                province = data.get("province", "") or ""
                city = data.get("city", "") or ""
                # 高德对无法精确归属的 IP 可能把 province/city 返回为空数组 []
                if isinstance(city, list):
                    city = city[0] if city else ""
                if isinstance(province, list):
                    province = province[0] if province else ""
                # 高德对私有/未知 IP 常返回"局域网"，无实际定位价值 → 视为空
                if city in ("局域网", "") or province in ("局域网", ""):
                    return {"city": "", "province": ""}
                # 高德直辖市的 city 可能为空/等于 province，兜底保持一致
                if not city:
                    city = province
                return {"city": city, "province": province, "ip": ip}
    except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError) as e:
        logger.debug(f"IP 定位服务不可用: {type(e).__name__}")
    return {"city": "", "province": ""}

async def _resolve_adcode(amap_key: str, name: str, client):
    """通过高德地理编码把城市名解析为 adcode"""
    try:
        resp = await client.get("https://restapi.amap.com/v3/geocode/geo", params={
            "key": amap_key, "address": name
        })
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "1" and data.get("geocodes"):
                return data["geocodes"][0].get("adcode")
    except Exception as e:
        logger.debug(f"adcode 查询失败: {type(e).__name__}")
    return None

@router.get("/weather")
async def get_weather(city: str, force: int = 0, current_user: dict = Depends(get_current_user)):
    await _require_map_qps(current_user)
    city = _san_city(city)
    import time as _time
    _t0 = _time.perf_counter()
    result = await _weather_impl(city, force)
    logger.info(f"[map-perf] weather city={city} total={_time.perf_counter() - _t0:.2f}s")
    return result


async def _weather_impl(city: str, force: int):
    # 天气走高德预报（extensions=all）：出行查的是今天/明天/后天的白天天气，
    # 不是当下实况（2026-09-10 主人定稿）。高频城市当日缓存同前。
    if not force:
        cached = await _city_cache_get(city, "weather")
        if cached is not None:
            logger.info(f"[map-perf] weather city={city} cache=hit")
            return cached
    amap_key = AMAP_API_KEY
    fallback = {"status": "1", "days": []}
    if not amap_key:
        return fallback

    try:
        # ---- 港澳特殊处理：用临近城市代替 ----
        fallback_map = {
            "香港": {"city": "深圳", "note": "（以下为深圳天气，香港临近仅供参考）"},
            "澳门": {"city": "珠海", "note": "（以下为珠海天气，澳门临近仅供参考）"},
        }

        candidates = weather_candidates(city)
        note = ""
        if city in fallback_map:
            fb = fallback_map[city]
            candidates = [fb["city"]]
            note = fb["note"]

        async with httpx.AsyncClient(timeout=MAP_API_TIMEOUT) as client:
            # 1) 依次尝试候选名称
            for cand in candidates:
                if not cand:
                    continue
                resp = await client.get(
                    "https://restapi.amap.com/v3/weather/weatherInfo",
                    params={"city": cand, "key": amap_key, "extensions": "all"}
                )
                if resp.status_code == 200:
                    data = resp.json()
                    out = _format_forecast(data, note)
                    if out is not None:
                        await _city_cache_put(city, "weather", out, force=bool(force))
                        return out

            # 2) 兜底：地理编码解析 adcode 后再查一次
            adcode = await _resolve_adcode(amap_key, city, client)
            if adcode:
                resp = await client.get(
                    "https://restapi.amap.com/v3/weather/weatherInfo",
                    params={"city": adcode, "key": amap_key, "extensions": "all"}
                )
                if resp.status_code == 200:
                    data = resp.json()
                    out = _format_forecast(data, note)
                    if out is not None:
                        await _city_cache_put(city, "weather", out, force=bool(force))
                        return out
    except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError):
        # 网络超时或无法连接时返回兜底数据
        return fallback
    except Exception as _err:
        logger.warning(f"天气上游异常，返回兜底: {type(_err).__name__}: {_err}")
        return fallback
    # 全部失败 → 兜底数据（不抛 502，前端可正常展示美食/景点）
    return fallback


def _format_forecast(data: dict, note: str):
    """把高德预报（extensions=all）压成 今天/明天/后天 三天白天天气；无有效预报返回 None"""
    if not (data.get("status") == "1" and data.get("forecasts")):
        return None
    casts = (data["forecasts"][0].get("casts") or [])[:3]
    if not casts:
        return None
    labels = ["今天", "明天", "后天"]
    days = []
    for i, c in enumerate(casts):
        days.append({
            "label": labels[i] if i < len(labels) else c.get("date", f"第{i+1}天"),
            "weather": c.get("dayweather") or c.get("nightweather") or "--",
            "temp": f"{c.get('nighttemp', '--')}~{c.get('daytemp', '--')}℃",
        })
    out = {"status": "1", "days": days}
    if note:
        out["_note"] = note
    return out

@router.post("/recommend")
async def get_recommend(city: str, force: int = 0, current_user: dict = Depends(get_current_user)):
    city = _san_city(city)
    import time as _time
    _t0 = _time.perf_counter()
    # 共享池命中：随机抽 3+3 直接返回（全员可读、不走 LLM 也不计费）；
    # force=1（刷新按钮）跳过读池，重新生成并并入池
    if not force:
        picked = await _recommend_pick_from_pool(city)
        if picked is not None:
            logger.info(f"[map-perf] recommend city={city} cache=pool_hit")
            return picked
    # 配额门禁（2026-09-07 审查 P1）：原先完全绕过 ensure_chat_allowed/日 token 限额，
    # 登录用户可刷 LLM 配额；LLM 调用期间占用并发槽位，finally 释放
    from ..services.chat_stream_ctx import ensure_chat_allowed
    from ..middleware.rate_limit import release_concurrent
    today = await ensure_chat_allowed(current_user)
    lease = current_user.get("_concurrent_lease", "")
    try:
        try:
            result = await _recommend_llm(city, username=current_user["username"])
        except Exception:
            # 上游畸形响应/网络异常时，用户没有得到有效推荐，试用额度必须归还。
            from ..core.quota import rollback_used_questions
            await rollback_used_questions(current_user["username"])
            raise
        if result is _DEFAULT_REC:
            # 上游失败/缺少 Key 时不能吞掉预留的试用额度，否则用户未得到真实
            # 推荐却被计一次可用次数。
            from ..core.quota import rollback_used_questions
            await rollback_used_questions(current_user["username"])
            return result
        # 形状校验（2026-09-10 复查 P2）：畸形但合法的 JSON 不得写入共享池
        if not (isinstance(result, dict) and isinstance(result.get("foods"), list)
                and isinstance(result.get("spots"), list)):
            result = _DEFAULT_REC
        if result is not _DEFAULT_REC:
            await _recommend_pool_put(city, result)
        # 2026-09-12 修复（外部复核 P1）：地图通道成功后计入日请求——原仅对话
        # 主链 finalize_answer 计数，本通道日请求上限可被绕过
        from ..middleware.rate_limit import update_daily_usage
        await update_daily_usage(current_user["username"], today, inc_request=1, inc_token=0)
        logger.info(f"[map-perf] recommend city={city} total={_time.perf_counter() - _t0:.2f}s "
                    f"cache={'force' if force else 'pool_miss_generate'}")
        return result
    finally:
        await release_concurrent(current_user["username"], lease)


async def _recommend_llm(city: str, username: str = "") -> dict:
    """推荐正文（LLM 调用与解析），由 /recommend 在配额门禁内调用

    username 供计量扣费（2026-09-09 审查 P1：原先只过门禁不扣费不计日 token）。
    """
    # 与主链路同款 Key 轮询（统一 chat_support 取 Key，杜绝多 Key 白名单不一致的 400）
    import time
    from ..services.chat_support import get_deepseek_key
    _t0 = time.perf_counter()
    deepseek_key = await get_deepseek_key()
    _t_key = time.perf_counter()

    # ===== 通用兜底数据（API不可用时返回；模块级单例 _DEFAULT_REC，handler 靠同一性判断是否写缓存）=====

    if not deepseek_key:
        return _DEFAULT_REC

    import random
    # 随机即可（2026-09-10 主人定稿）：不强制风格差异，用 temperature 抖动自然变化；
    # temperature 仅 DeepSeek 侧携带（qwen 兜底时由 post_chat_completion 剥离防 400）
    prompt = f"""
你是一个本地美食和旅游专家。请为「{city}」推荐 3 道特色美食和 3 个必去景点。
要求：
1. 每次推荐的内容要多样化，避免千篇一律。
2. 只返回 JSON 格式，不要有其他文字。
格式：{{"foods":["美食1","美食2","美食3"], "spots":["景点1","景点2","景点3"]}}
"""
    from ..core.config import DEEPSEEK_MODEL
    # payload 对齐主链路成功形态：无 max_tokens（部分网关拒绝这些参数返回 400）
    payload = {
        "model": DEEPSEEK_MODEL,
        "temperature": round(random.uniform(0.9, 1.3), 2),
        "messages": [{"role": "user", "content": prompt}],
    }
    # 带降级（flash 优先：轻量结构化任务生成快 3 倍+，主模型兜底——2026-09-09 埋点结论）
    from ..services.chat_support import post_chat_completion
    from ..core.concurrency import llm_semaphore
    try:
        # 纳入全局 LLM 并发闸（2026-09-10 审查 P2：与其他 LLM 出口口径统一）
        async with llm_semaphore:
            ok, data = await post_chat_completion(payload, timeout=HTTP_TIMEOUT_MEDIUM,
                                                  prefer_flash=True, preferred_timeout=8)
        _t_llm = time.perf_counter()
        if not ok:
            logger.warning(f"[map-perf] recommend city={city} 上游失败 耗时={_t_llm - _t0:.2f}s (key={_t_key - _t0:.2f}s)")
            return _DEFAULT_REC
        content = data["choices"][0]["message"]["content"]
        # 计量扣费（2026-09-09 审查 P1）：/recommend 原先只过配额门禁，不扣费不计日 token
        _usage = data.get("usage") or {}
        if username and (_usage.get("prompt_tokens") or _usage.get("completion_tokens")):
            from ..services.llm_streaming import record_token_usage
            record_token_usage(_usage, username, "")
        # LLM 可能返回 ```json 包裹：走 _extract_json 容错解析
        from ..agents.sub_agents import _extract_json
        result = _extract_json(content)
        logger.info(
            f"[map-perf] recommend city={city} total={time.perf_counter() - _t0:.2f}s "
            f"(key={_t_key - _t0:.2f}s llm={_t_llm - _t_key:.2f}s parse={time.perf_counter() - _t_llm:.2f}s) "
            f"model={data.get('model', '?')} tokens={(_usage.get('total_tokens') or 0)}")
        return result
    except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError, httpx.HTTPStatusError):
        logger.warning(f"[map-perf] recommend city={city} 上游异常，返回兜底")
        return _DEFAULT_REC
    except Exception as e:
        logger.warning(f"recommend 解析失败（返回兜底）: {str(e)[:120]}")
        return _DEFAULT_REC


@router.post("/route")
async def plan_route(req: RouteRequest, current_user: dict = Depends(get_current_user)):
    """规划多段路线（支持高铁/飞机/火车/公交组合），返回每段的模式、起止城市、耗时"""
    # 配额门禁（2026-09-07 审查 P1）：同 /recommend，原先绕过全部限额直烧 LLM token
    from ..services.chat_stream_ctx import ensure_chat_allowed
    from ..middleware.rate_limit import release_concurrent
    today = await ensure_chat_allowed(current_user)
    lease = current_user.get("_concurrent_lease", "")
    try:
        result = await _plan_route_llm(req.departure, req.destination, username=current_user["username"])
        from ..middleware.rate_limit import update_daily_usage
        await update_daily_usage(current_user["username"], today, inc_request=1, inc_token=0)
        return result
    except Exception:
        from ..core.quota import rollback_used_questions
        await rollback_used_questions(current_user["username"])
        raise
    finally:
        await release_concurrent(current_user["username"], lease)


async def _plan_route_llm(departure: str, destination: str, username: str = "") -> dict:
    """路线规划正文（LLM 调用与解析），由 /route 在配额门禁内调用

    username 供计量扣费（2026-09-09 审查 P1：原先只过门禁不扣费不计日 token）。
    """
    # Key 取法统一（2026-09-07 审查 P2）：与 /recommend 一致走池化轮询，不再裸读 env
    from ..services.chat_support import get_deepseek_key
    deepseek_key = await get_deepseek_key()
    if not deepseek_key:
        raise HTTPException(503, "路线规划服务暂不可用")
    prompt = f"""你是交通路线规划专家。请为从「{departure}」到「{destination}」规划合理的交通路线。

要求：
1. 根据两地距离选择合适的主要交通方式：
   - < 200km: 高铁或自驾
   - 200-800km: 高铁优先
   - 800-1500km: 飞机+高铁组合
   - > 1500km: 飞机
2. 如果距离适中，可以规划多段路线（如飞机到某城市再转高铁到目的地）
3. 每段路线必须包含：出发城市、到达城市、交通方式、预计耗时（分钟）、距离（公里）
4. 交通方式用：高铁/飞机/火车/公交/自驾

只返回JSON，格式：
{{
  "legs": [
    {{"from": "北京", "to": "广州", "mode": "飞机", "duration_min": 180, "distance_km": 1967}},
    {{"from": "广州", "to": "珠海", "mode": "高铁", "duration_min": 60, "distance_km": 116}}
  ],
  "total_duration_min": 240,
  "total_distance_km": 2083,
  "recommendation": "建议先飞广州，再转高铁到珠海，全程约4小时"
}}"""
    from ..core.config import (
        DEEPSEEK_MODEL,
        apply_llm_request_options,
        llm_endpoint,
    )
    from ..core.concurrency import llm_semaphore
    base_url, api_key = llm_endpoint(DEEPSEEK_MODEL, deepseek_key)
    async with llm_semaphore:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_MEDIUM) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=apply_llm_request_options({
                    "model": DEEPSEEK_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                    "max_tokens": 500,
                }, DEEPSEEK_MODEL)
            )
            if resp.status_code != 200:
                raise HTTPException(502, "DeepSeek API error")
            data = resp.json()
        # 计量扣费（2026-09-09 审查 P1）：/route 原先只过配额门禁，不扣费不计日 token
        _usage = data.get("usage") or {}
        if username and (_usage.get("prompt_tokens") or _usage.get("completion_tokens")):
            from ..services.llm_streaming import record_token_usage
            record_token_usage(_usage, username, "")
        try:
            import json as _json
            content = data["choices"][0]["message"]["content"]
            return _json.loads(content)
        except Exception:  # noqa: BLE001 — 解析失败回退直答路线（不泄露内部错误）
            return {"legs": [{"from": departure, "to": destination, "mode": "高铁", "duration_min": 120, "distance_km": 500}], "total_duration_min": 120, "total_distance_km": 500, "recommendation": f"从{departure}到{destination}建议乘坐高铁"}


@router.post("/amap-route")
async def amap_route(req: RouteRequest, current_user: dict = Depends(get_current_user)):
    """高德路线入口：按用户限流与并发租约，避免多接口请求被匿名/单账号刷爆。"""
    from ..middleware.rate_limit import (
        check_qps, check_concurrent, release_concurrent, update_daily_usage,
    )
    username = current_user["username"]
    role = current_user.get("role", "user")
    if not await check_qps(username, role):
        raise HTTPException(429, "请求过于频繁，请稍后再试")
    lease = await check_concurrent(username, role)
    if lease is None:
        raise HTTPException(429, "并发请求过多，请稍后再试")
    try:
        result = await _plan_amap_route(req)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        await update_daily_usage(username, today, inc_request=1, inc_token=0)
        return result
    finally:
        await release_concurrent(username, lease)


async def _plan_amap_route(req: RouteRequest) -> dict:
    """路由层薄封装：缺 Key 时 fail loudly，业务解析在 map_amap 模块。"""
    if not AMAP_API_KEY:
        raise HTTPException(503, "路线规划服务暂不可用")
    try:
        return await plan_amap_route(
            AMAP_API_KEY, req.departure, req.destination
        )
    except ValueError as exc:
        raise HTTPException(502, str(exc)) from exc
    except httpx.HTTPError as exc:
        # API-3（2026-09-20 审查）：raise_for_status/超时抛 HTTPError 原先不接 → 裸 500；
        # 且其 str 含完整请求 URL（key 在查询串里），日志只记类型名，对外固定文案。
        logger.warning(f"高德路线上游请求失败: err_type={type(exc).__name__}")
        raise HTTPException(502, "路线规划服务暂不可用，请稍后再试") from exc


@router.get("/geojson")
async def get_geojson(adcode: str = "100000", current_user: dict = Depends(get_current_user)):
    """代理 GeoJSON 地图数据，绕过阿里云 DataV 的 Referer 防盗链（需登录）"""
    await _require_map_qps(current_user)
    # P3 修复：adcode 只允许 6 位数字，防 URL 路径拼接
    if not re.fullmatch(r"\d{6}", adcode):
        raise HTTPException(400, "adcode 必须为 6 位数字")
    if adcode == "100000":
        url = "https://geo.datav.aliyun.com/areas_v3/bound/100000_full.json"
    else:
        url = f"https://geo.datav.aliyun.com/areas_v3/bound/{adcode}_full.json"
    async with httpx.AsyncClient(timeout=MAP_API_TIMEOUT) as client:
        resp = await client.get(url, headers={"User-Agent": "AgentGateway/1.0"})
        if resp.status_code != 200:
            raise HTTPException(502, f"GeoJSON data fetch failed (HTTP {resp.status_code})")
        return resp.json()

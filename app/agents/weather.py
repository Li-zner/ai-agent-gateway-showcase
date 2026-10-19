"""天气工具（高德 geocode + 实时天气）

2026-09-10 自 tools.py 拆出（tools.py 超 600 行硬限，按子系统边界切分）；
tools.py 保留 re-export，外部 import 路径不变。
"""

import httpx

from ..core.logging import setup_logging
from ..core.config import MAP_API_TIMEOUT

logger = setup_logging()


def _geocode_contains(candidate: str, geocode: dict) -> bool:
    """geocode 包含性守卫：已下沉 core.place_extract.geocode_contains 共享
    （2026-09-29 路线规划接入同款，防两处定义漂移），此处保留薄转发。"""
    from ..core.place_extract import geocode_contains
    return geocode_contains(candidate, geocode)


def _normalize_forecast(forecasts: list) -> list:
    """把高德 forecasts 结构压缩成前端和 LLM 都易读的三日预报。"""
    if not forecasts:
        return []
    casts = forecasts[0].get("casts") or []
    normalized = []
    for cast in casts[:3]:
        normalized.append({
            "date": cast.get("date", ""),
            "day_weather": cast.get("dayweather", ""),
            "night_weather": cast.get("nightweather", ""),
            "day_temp": cast.get("daytemp", ""),
            "night_temp": cast.get("nighttemp", ""),
            "day_wind": cast.get("daywind", ""),
            "night_wind": cast.get("nightwind", ""),
        })
    return normalized


async def fetch_weather_async(city: str, depth: int = 0):
    """异步调用高德天气 API：返回实时天气和未来三天预报。

    depth 防 geocode 守卫递归：默认城市再被守卫拒绝时直接报错，不再递归。
    """
    # 2026-09-11 规则审查收编：与 routes/map_api 一致走 config 统一导出
    from ..core.config import AMAP_API_KEY
    amap_key = AMAP_API_KEY
    if not amap_key:
        return {"error": "缺少高德 API Key"}
    try:
        # 2026-09-30 校准收敛（B3）：同服务（restapi.amap.com）与 map_api 其余
        # 五处统一用 MAP_API_TIMEOUT，原先这里用 SHORT(5s) 各改各的
        async with httpx.AsyncClient(timeout=MAP_API_TIMEOUT) as client:
            # 1. 查城市编码
            geo_resp = await client.get(
                "https://restapi.amap.com/v3/geocode/geo",
                params={"key": amap_key, "address": city}
            )
            geo_data = geo_resp.json()
            if geo_data["status"] != "1" or not geo_data["geocodes"]:
                return {"error": f"未找到城市: {city}"}
            geo = geo_data["geocodes"][0]
            if not _geocode_contains(city, geo):
                # 高德模糊匹配到了不相干地区（残句/垃圾候选），宁可用默认城市也不答非所问
                from ..core.place_extract import default_city
                fallback = default_city()
                logger.info(f"geocode 模糊匹配守卫拦截: {city!r} → {geo.get('city')!r}，改查默认城市 {fallback!r}")
                if depth >= 1:
                    return {"error": f"未找到城市: {city}"}
                return await fetch_weather_async(fallback, depth + 1)
            adcode = geo["adcode"]

            # 2. 查天气
            weather_resp = await client.get(
                "https://restapi.amap.com/v3/weather/weatherInfo",
                params={"key": amap_key, "city": adcode, "extensions": "all"}
            )
            weather_data = weather_resp.json()
            if weather_data["status"] != "1":
                return {"error": "天气查询失败"}
            forecast = _normalize_forecast(weather_data.get("forecasts") or [])
            lives = weather_data.get("lives") or []
            if not lives:
                # all 接口可能只返回 forecasts 而不返回 lives，用首日预报作实时兜底。
                if forecast:
                    today = forecast[0]
                    return {
                        "city": city,
                        "temperature": today["day_temp"] or today["night_temp"],
                        "weather": today["day_weather"] or today["night_weather"],
                        "wind": today["day_wind"] or today["night_wind"],
                        "forecast": forecast,
                    }
                return {"error": f"{city} 暂无天气数据"}
            live = lives[0]
            # 港澳台：高德无天气数据（lives 缺字段）；geo 解析会落邻城（港→深/澳→珠），
            # 此时如实标注数据来源城市，由访客自行参考
            if not live.get("weather") or not live.get("temperature"):
                return {"error": f"{city} 暂无天气数据"}
            return {
                "city": live.get("city", city),
                "temperature": live["temperature"],
                "weather": live["weather"],
                "wind": live["winddirection"],
                "forecast": forecast,
            }
    except Exception as e:
        # 异常 str 可能带含 key 的完整 URL，用户侧只给通用文案（细节进日志）
        logger.warning(f"天气查询异常: {type(e).__name__}: {e}")
        return {"error": "天气查询失败"}

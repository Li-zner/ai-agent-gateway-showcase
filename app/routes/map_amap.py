"""高德路线抓取与结果解析。"""
from __future__ import annotations

import httpx

from ..core.config import MAP_API_TIMEOUT


async def _amap_json(client: httpx.AsyncClient, url: str, params: dict) -> dict:
    """调用高德接口并返回 JSON；上游错误交给调用方统一处理。"""
    resp = await client.get(url, params=params)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise ValueError("高德接口返回非对象")
    return data


async def _gather_geocodes(
    client: httpx.AsyncClient,
    amap_key: str,
    departure: str,
    destination: str,
) -> tuple[dict, dict]:
    """顺序调用同一 AsyncClient，避免连接池内并发取消导致资源告警。

    返回前过 geocode 包含性守卫（2026-09-29）：高德对垃圾输入也会模糊匹配，
    weather 侧 2026-09-10 就因此答非所问过（"我需要具体"→枣庄薛城区）——
    路线规划此前漏同款，"从我家到你家"会被规划成两个随机城市。
    """
    from ..core.place_extract import geocode_contains

    dep = await _amap_json(
        client,
        "https://restapi.amap.com/v3/geocode/geo",
        {"key": amap_key, "address": departure, "city": departure},
    )
    dest = await _amap_json(
        client,
        "https://restapi.amap.com/v3/geocode/geo",
        {"key": amap_key, "address": destination, "city": destination},
    )
    if dep.get("status") != "1" or dest.get("status") != "1":
        raise ValueError("地理编码失败")
    for label, candidate, geo in (("出发地", departure, dep),
                                  ("目的地", destination, dest)):
        geocodes = geo.get("geocodes") or []
        if not geocodes or not geocode_contains(candidate, geocodes[0]):
            raise ValueError(f"无法识别{label}: {candidate[:20]}")
    return dep, dest


async def fetch_amap_route_data(amap_key: str, departure: str, destination: str) -> dict:
    """抓取地理编码、驾车、公交、骑行和步行数据。"""
    async with httpx.AsyncClient(timeout=MAP_API_TIMEOUT) as client:
        # status/守卫校验已收进 _gather_geocodes（2026-09-29），此处不重复
        geo_dep, geo_dest = await _gather_geocodes(
            client, amap_key, departure, destination
        )
        dep_loc = geo_dep["geocodes"][0]["location"]
        dest_loc = geo_dest["geocodes"][0]["location"]
        return {
            "dep_loc": dep_loc,
            "dest_loc": dest_loc,
            "driving": await _amap_json(
                client,
                "https://restapi.amap.com/v3/direction/driving",
                {"key": amap_key, "origin": dep_loc, "destination": dest_loc,
                 "strategy": 0, "extensions": "all"},
            ),
            "transit": await _amap_json(
                client,
                "https://restapi.amap.com/v3/direction/transit/integrated",
                {"key": amap_key, "origin": dep_loc, "destination": dest_loc,
                 "city": departure, "cityd": destination, "strategy": 0,
                 "extensions": "all"},
            ),
            "bicycling": await _amap_json(
                client,
                "https://restapi.amap.com/v3/direction/bicycling",
                {"key": amap_key, "origin": dep_loc, "destination": dest_loc},
            ),
            "walking": await _amap_json(
                client,
                "https://restapi.amap.com/v3/direction/walking",
                {"key": amap_key, "origin": dep_loc, "destination": dest_loc},
            ),
        }


def _minutes(seconds) -> int:
    """把秒转换为分钟，缺失值按 0 处理。"""
    return round(int(seconds or 0) / 60)


def _as_dict(value) -> dict:
    """字段类型防御：上游缺字段或返回 null 时按空对象处理。"""
    return value if isinstance(value, dict) else {}


def _kilometers(meters) -> float:
    """把米转换为公里，缺失值按 0 处理。"""
    return round(int(meters or 0) / 1000, 1)


def _railway_mode(name: str) -> str:
    """按车次字头归类：G→高铁、D→动车、其余（K/T/Z/纯数字/空）→火车。"""
    if name[:1].upper() == "G":
        return "高铁"
    if name[:1].upper() == "D":
        return "动车"
    return "火车"


def _parse_transit_result(transit: dict) -> list:
    """把高德公交/火车结果转换为统一 legs 结构。"""
    raw_transits = _as_dict(transit.get("route")).get("transits") or []
    transits = raw_transits[:3] if isinstance(raw_transits, list) else []
    items = []
    for item in transits:
        item = _as_dict(item)
        legs = []
        segments = item.get("segments") or []
        for segment in segments if isinstance(segments, list) else []:
            segment = _as_dict(segment)
            if "railway" in segment:
                railway = _as_dict(segment["railway"])
                legs.append({
                    # 车次字头判断（2026-09-29）：原逻辑"名字含'火车'否则高铁"会把
                    # K/T/Z 字头普速全部误标"高铁"；按高德车次命名规范 G=高铁、
                    # D=动车、其余（K/T/Z/纯数字）=火车
                    "mode": _railway_mode(str(railway.get("name", ""))),
                    "from": railway.get("departure_station", {}).get("name", ""),
                    "to": railway.get("arrival_station", {}).get("name", ""),
                    "duration_min": _minutes(railway.get("time")),
                    "distance_km": _kilometers(railway.get("distance")),
                })
            elif "bus" in segment:
                buslines = _as_dict(segment["bus"]).get("buslines") or [{}]
                bus_list = buslines if isinstance(buslines, list) and buslines else [{}]
                bus = _as_dict(bus_list[0])
                legs.append({
                    "mode": "公交",
                    "from": bus.get("departure_stop", {}).get("name", ""),
                    "to": bus.get("arrival_stop", {}).get("name", ""),
                    "duration_min": _minutes(segment.get("duration")),
                    "distance_km": _kilometers(segment.get("distance")),
                })
            elif "walking" in segment:
                legs.append({
                    "mode": "步行",
                    "duration_min": _minutes(segment.get("duration")),
                    "distance_km": _kilometers(segment.get("distance")),
                })
        items.append({
            "legs": legs,
            "total_duration_min": sum(leg.get("duration_min", 0) for leg in legs),
            "total_distance_km": round(
                sum(leg.get("distance_km", 0) for leg in legs), 1
            ),
        })
    return items


def _apply_short_route(result: dict, name: str, payload: dict) -> None:
    """填充骑行或步行路径，无有效结果时保持字段缺失。"""
    paths = _as_dict(payload.get("route")).get("paths") or []
    if payload.get("status") != "1" or not isinstance(paths, list) or not paths:
        return
    path = paths[0]
    result[name] = {
        "distance_km": _kilometers(path.get("distance")),
        "duration_min": _minutes(path.get("duration")),
    }


def build_amap_route_result(departure: str, destination: str, data: dict) -> dict:
    """把高德原始响应转换为控制台稳定消费的结构。"""
    result = {
        "departure": departure,
        "destination": destination,
        "departure_location": data["dep_loc"],
        "destination_location": data["dest_loc"],
    }
    driving = _as_dict(data.get("driving"))
    paths = _as_dict(driving.get("route")).get("paths") or []
    if driving.get("status") == "1" and isinstance(paths, list) and paths:
        path = paths[0]
        result["driving"] = {
            "distance_km": _kilometers(path.get("distance")),
            "duration_min": _minutes(path.get("duration")),
            "tolls": path.get("tolls", "0"),
            "polyline": (path.get("steps") or [{}])[0].get("polyline", ""),
        }
    transit = _as_dict(data.get("transit"))
    if transit.get("status") == "1":
        items = _parse_transit_result(transit)
        if items:
            result["transit"] = items
    _apply_short_route(result, "bicycling", _as_dict(data.get("bicycling")))
    _apply_short_route(result, "walking", _as_dict(data.get("walking")))
    if not any(name in result for name in ("driving", "transit", "bicycling", "walking")):
        raise ValueError("高德未返回任何可用路线")
    return result


async def plan_amap_route(
    amap_key: str,
    departure: str,
    destination: str,
) -> dict:
    """抓取并解析高德路线。"""
    data = await fetch_amap_route_data(amap_key, departure, destination)
    return build_amap_route_result(departure, destination, data)

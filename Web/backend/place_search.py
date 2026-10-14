"""Baidu place/address lookup. Credentials stay on the server; results use BD-09."""

from __future__ import annotations

import math
import os
import re
from pathlib import Path

import requests
from dotenv import dotenv_values


class PlaceSearchError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def _key() -> str:
    key = os.getenv("BAIDU_WEB_SERVICE_AK", "").strip()
    if not key:
        key = str(dotenv_values(Path(__file__).with_name(".env")).get("BAIDU_WEB_SERVICE_AK") or "").strip()
    if not key:
        raise PlaceSearchError("尚未配置地点搜索服务，请在后端配置服务端 AK。", 503)
    return key


def _request(path: str, params: dict) -> dict:
    try:
        response = requests.get(
            f"https://api.map.baidu.com/{path}",
            params={**params, "ak": _key(), "output": "json"},
            timeout=(5, 12),
        )
        response.raise_for_status()
        payload = response.json()
    except requests.Timeout:
        raise PlaceSearchError("地点搜索超时，请稍后重试。", 504) from None
    except (requests.RequestException, ValueError):
        raise PlaceSearchError("暂时无法连接地点搜索服务，请稍后重试。") from None
    if not isinstance(payload, dict):
        raise PlaceSearchError("地点搜索服务返回了无法识别的数据。")
    status = payload.get("status")
    if status == 0:
        return payload
    if status == 240:
        raise PlaceSearchError("百度 AK 未开启此服务（240）。请在控制台开启地点检索、地理编码和全球逆地理编码服务。", 503)
    if status in (3, 5, 101, 102, 200, 201, 202, 203, 210, 211):
        raise PlaceSearchError("百度 AK 校验失败，请检查服务端应用类型、IP 白名单或签名配置。", 503)
    if status in (4, 302, 401):
        raise PlaceSearchError("地点搜索调用额度或并发已超限，请稍后重试。", 429)
    raise PlaceSearchError("地点搜索服务未能完成请求，请检查地点名称后重试。")


def _candidate(item: dict, *, name: str = "", precision: bool = False) -> dict | None:
    location = item.get("location")
    if not isinstance(location, dict):
        return None
    lat, lng = location.get("lat"), location.get("lng")
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in (lat, lng)):
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    candidate = {
        "name": item.get("name") or name,
        "address": item.get("address") or item.get("formatted_address") or "",
        "uid": item.get("uid") or "",
        "location": {"lat": lat, "lng": lng},
        "coordinate_system": "bd09",
    }
    if precision:
        candidate["precision"] = {
            "precise": item.get("precise"), "confidence": item.get("confidence"), "level": item.get("level"),
        }
    return candidate


def _geocode(query: str, region: str) -> list[dict]:
    if len(query.encode("utf-8")) > 128:
        raise PlaceSearchError("详细地址过长，请缩短到 128 字节以内（通常约 42 个汉字）。", 422)
    payload = _request("geocoding/v3/", {"address": query, "city": region, "extension_poi_infos": "true"})
    result = payload.get("result")
    if not isinstance(result, dict):
        return []
    infos = payload.get("poi_infos")
    info = infos[0] if isinstance(infos, list) and infos and isinstance(infos[0], dict) else {}
    item = _candidate({**info, **result}, name=query, precision=True)
    return [item] if item else []


def _places(query: str, region: str) -> list[dict]:
    payload = _request("place/v3/region", {"query": query, "region": region, "scope": 1, "page_size": 10})
    if payload.get("result_type") in ("city_type", "region_type"):
        return []
    results = payload.get("results", [])
    if not isinstance(results, list):
        raise PlaceSearchError("地点搜索服务返回了无法识别的数据。")
    return [candidate for item in results[:10] if isinstance(item, dict) and (candidate := _candidate(item))]


def search_places(query: str, region: str = "常州市") -> dict:
    query, region = query.strip(), region.strip() or "全国"
    if not query:
        raise PlaceSearchError("请输入地点名称或详细地址。", 422)
    # Structured street addresses use geocoding; POI names use Place Search.
    address = bool(re.search(r"\d+\s*号|路|街|巷|弄|交叉口|交叉路口", query))
    primary, fallback = (_geocode, _places) if address else (_places, _geocode)
    results = primary(query, region)
    if not results:
        results = fallback(query, region)
    return {"results": results}


def _wgs84_location(latitude: float, longitude: float) -> str:
    """Validate browser geolocation coordinates and format Baidu's lat,lng input."""
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        for value in (latitude, longitude)
    ) or not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise PlaceSearchError("当前位置的经纬度无效，请重新定位。", 422)
    return f"{latitude},{longitude}"


def search_nearby_places(query: str, latitude: float, longitude: float) -> dict:
    """Search POIs within 1 km of a browser WGS84 location."""
    if not isinstance(query, str) or not query.strip():
        raise PlaceSearchError("请输入要搜索的附近地点名称或类别。", 422)
    query = query.strip()
    if len(query.encode("utf-8")) > 128:
        raise PlaceSearchError("搜索关键词过长，请缩短到 128 字节以内。", 422)
    location = _wgs84_location(latitude, longitude)
    payload = _request("place/v3/around", {
        "query": query,
        "location": location,
        "coord_type": 1,
        "radius": 1000,
        "radius_limit": "true",
        "scope": 2,
        "page_size": 10,
    })
    results = payload.get("results", [])
    if not isinstance(results, list):
        raise PlaceSearchError("地点搜索服务返回了无法识别的数据。")
    candidates = []
    for item in results[:10]:
        if not isinstance(item, dict) or not (candidate := _candidate(item)):
            continue
        detail_info = item.get("detail_info")
        distance = detail_info.get("distance") if isinstance(detail_info, dict) else None
        if isinstance(distance, (int, float)) and not isinstance(distance, bool) and math.isfinite(distance) and distance >= 0:
            candidate["distance_m"] = distance
        candidates.append(candidate)
    return {"results": candidates}


def _safe_text(value: object, max_bytes: int) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    try:
        byte_count = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return ""
    if not value or byte_count > max_bytes or any(ord(char) < 32 for char in value):
        return ""
    return value


def _distance_m(value: object) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
            return None
    elif not isinstance(value, (int, float)):
        return None
    try:
        distance = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(distance) or distance < 0:
        return None
    return int(distance) if distance.is_integer() else distance


def _reverse_place_name(result: dict) -> str:
    regions = result.get("poiRegions")
    if isinstance(regions, list):
        for region in regions:
            if not isinstance(region, dict):
                continue
            name = _safe_text(region.get("name"), 128)
            direction = _safe_text(region.get("direction_desc"), 128)
            if name and (not direction or "内" in direction or _distance_m(region.get("distance")) == 0):
                return name

    pois = result.get("pois")
    if isinstance(pois, list):
        nearby_names = []
        for index, poi in enumerate(pois):
            if not isinstance(poi, dict):
                continue
            name = _safe_text(poi.get("name"), 128)
            distance = _distance_m(poi.get("distance"))
            if name and (distance is None or distance <= 1000):
                nearby_names.append((distance is None, distance if distance is not None else float("inf"), index, name))
        if nearby_names:
            return min(nearby_names)[3]

    businesses = result.get("business_info")
    if isinstance(businesses, list):
        for business in businesses:
            if isinstance(business, dict) and (name := _safe_text(business.get("name"), 128)):
                return name

    component = result.get("addressComponent")
    if isinstance(component, dict):
        for field in ("street", "town", "district"):
            if name := _safe_text(component.get(field), 128):
                return name
    return ""


def _reverse_nearby_results(result: dict) -> list[dict]:
    pois = result.get("pois")
    if not isinstance(pois, list):
        return []
    candidates = []
    for poi in pois:
        if not isinstance(poi, dict):
            continue
        name = _safe_text(poi.get("name"), 128)
        point = poi.get("point")
        if not name or not isinstance(point, dict):
            continue
        distance = _distance_m(poi.get("distance"))
        if distance is not None and distance > 1000:
            continue
        candidate = _candidate({
            "name": name,
            "address": _safe_text(poi.get("addr"), 512),
            "uid": _safe_text(poi.get("uid"), 128),
            "location": {"lat": point.get("y"), "lng": point.get("x")},
        })
        if candidate is None:
            continue
        if distance is not None:
            candidate["distance_m"] = distance
        candidates.append(candidate)
        if len(candidates) == 10:
            break
    return candidates


def reverse_geocode(latitude: float, longitude: float) -> dict:
    """Resolve a browser WGS84 location to an address and local POI context."""
    location = _wgs84_location(latitude, longitude)
    payload = _request("reverse_geocoding/v3/", {
        "location": location,
        "coordtype": "wgs84ll",
        "extensions_poi": 1,
        "radius": 1000,
    })
    result = payload.get("result")
    if not isinstance(result, dict):
        raise PlaceSearchError("逆地理编码服务返回了无法识别的数据。")
    address = ""
    for field in ("formatted_address_poi", "formatted_address"):
        if address := _safe_text(result.get(field), 512):
            break
    if not address:
        raise PlaceSearchError("当前位置没有可用的地址信息，请重新定位。")
    return {
        "address": address,
        "place_name": _reverse_place_name(result),
        "nearby_results": _reverse_nearby_results(result),
    }

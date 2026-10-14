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


def reverse_geocode(latitude: float, longitude: float) -> dict:
    """Resolve a browser WGS84 location to a readable address."""
    location = _wgs84_location(latitude, longitude)
    payload = _request("reverse_geocoding/v3/", {
        "location": location,
        "coordtype": "wgs84ll",
        "extensions_poi": 1,
    })
    result = payload.get("result")
    if not isinstance(result, dict):
        raise PlaceSearchError("逆地理编码服务返回了无法识别的数据。")
    for field in ("formatted_address_poi", "formatted_address"):
        address = result.get(field)
        if isinstance(address, str) and address.strip():
            return {"address": address.strip()}
    raise PlaceSearchError("当前位置没有可用的地址信息，请重新定位。")

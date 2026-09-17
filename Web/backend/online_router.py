"""Online road reference route from an OSRM routing service."""

from __future__ import annotations

import math
import os
import time
from functools import lru_cache

import requests

from road_planner import MAX_SNAP_M, RouteUnavailable


class OnlineServiceUnavailable(Exception):
    pass


def straight_distance_m(start_lat: float, start_lon: float, end_lat: float, end_lon: float) -> float:
    radius = 6_371_008.8
    latitude_delta = math.radians(end_lat - start_lat)
    longitude_delta = math.radians(end_lon - start_lon)
    angle = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(math.radians(start_lat))
        * math.cos(math.radians(end_lat))
        * math.sin(longitude_delta / 2) ** 2
    )
    return 2 * radius * math.asin(min(1.0, math.sqrt(angle)))


@lru_cache(maxsize=128)
def _fetch_route(url: str, ten_minute_bucket: int) -> dict:
    del ten_minute_bucket  # Part of the cache key; refresh results after ten minutes.
    try:
        response = requests.get(
            url,
            params={"overview": "full", "geometries": "geojson", "steps": "true"},
            headers={"User-Agent": "LowAltitudeRoutePrototype/0.2 (road reference comparison)"},
            timeout=(5, 15),
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as error:
        raise OnlineServiceUnavailable("在线道路服务暂时不可用，请稍后重试或切换到本地。") from error
    if not isinstance(payload, dict):
        raise OnlineServiceUnavailable("在线道路服务返回了无法识别的数据。")
    return payload


def plan_online_route(
    start_lat: float, start_lon: float, end_lat: float, end_lon: float, clearance_m: float = 2.0
) -> dict:
    del clearance_m  # OSRM does not evaluate building clearance.
    direct_distance = straight_distance_m(start_lat, start_lon, end_lat, end_lon)
    if direct_distance > 10_000:
        raise RouteUnavailable("当前原型只支持两点相距 10 公里以内的道路规划。")

    base_url = os.getenv("ONLINE_ROUTER_URL", "https://router.project-osrm.org").rstrip("/")
    coordinates = f"{start_lon:.7f},{start_lat:.7f};{end_lon:.7f},{end_lat:.7f}"
    url = f"{base_url}/route/v1/driving/{coordinates}"
    payload = _fetch_route(url, int(time.time() // 600))
    if payload.get("code") != "Ok" or not payload.get("routes"):
        raise RouteUnavailable("在线道路网络未找到可用路线，请调整 A/B 点位。")

    try:
        route = payload["routes"][0]
        waypoints = payload["waypoints"]
        start_snap = float(waypoints[0]["distance"])
        end_snap = float(waypoints[-1]["distance"])
        road_coordinates = route["geometry"]["coordinates"]
        road_distance = float(route["distance"])
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise OnlineServiceUnavailable("在线道路服务返回了不完整的路线。") from error
    if not road_coordinates or not all(math.isfinite(value) for value in (start_snap, end_snap, road_distance)):
        raise OnlineServiceUnavailable("在线道路服务返回了无效的路线。")
    if start_snap > MAX_SNAP_M or end_snap > MAX_SNAP_M:
        distance = max(start_snap, end_snap)
        raise RouteUnavailable(f"端点距在线道路约 {distance:.1f} 米，超过 {MAX_SNAP_M:.0f} 米接入范围。")

    road_names: list[str] = []
    for leg in route.get("legs", []):
        for step in leg.get("steps", []):
            name = step.get("name") or step.get("ref")
            if name and name not in road_names:
                road_names.append(name)

    total_distance = road_distance + start_snap + end_snap
    return {
        "geometry": {
            "type": "LineString",
            "coordinates": [[start_lon, start_lat], *road_coordinates, [end_lon, end_lat]],
        },
        "distance_m": round(total_distance, 1),
        "straight_distance_m": round(direct_distance, 1),
        "start_snap_m": round(start_snap, 1),
        "end_snap_m": round(end_snap, 1),
        "road_names": road_names,
        "building_clearance_m": None,
        "data_source": "OSRM 在线路线（机动车道路，基于 OpenStreetMap）",
    }

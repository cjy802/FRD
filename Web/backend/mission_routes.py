"""Combine ordered, user-selected anchors with generated road-route vertices."""

from __future__ import annotations

import math
from typing import Sequence

from online_router import straight_distance_m


# Planning points are independent of drone-specific waypoint export limits.
# This ceiling only prevents unbounded work in one HTTP request.
MAX_MISSION_ANCHORS = 1_000
MAX_MISSION_VERTICES = 10_000


class MissionRouteError(ValueError):
    """A segment planner returned geometry that cannot represent this mission."""


class MissionRouteLimitError(MissionRouteError):
    """The generated geometry exceeds the supported mission resource budget."""


def _endpoint(anchor: dict) -> list[float]:
    return [anchor["longitude"], anchor["latitude"]]


def _valid_coordinate(point: object) -> bool:
    return (
        isinstance(point, (list, tuple))
        and len(point) == 2
        and all(isinstance(value, (int, float)) and math.isfinite(value) for value in point)
        and -180 <= point[0] <= 180
        and -90 <= point[1] <= 90
    )


def combine_mission_segments(anchors: Sequence[dict], routes: Sequence[dict], speed_kmh: float) -> dict:
    """Join adjacent planned paths, retaining each anchor exactly once.

    Segment start/end indices refer to the full geometry. Interior indices in
    each span are generated road vertices, rather than manually chosen anchors.
    """
    if len(anchors) < 2 or len(routes) != len(anchors) - 1:
        raise MissionRouteError("任务航段数量与主点数量不匹配。")

    coordinates: list[list[float]] = []
    anchor_indices: list[int] = []
    segments: list[dict] = []
    road_names: list[str] = []
    total_distance_m = 0.0
    total_straight_m = 0.0

    for index, (start, end, route) in enumerate(zip(anchors, anchors[1:], routes)):
        try:
            raw_points = route["geometry"]["coordinates"]
            distance_m = float(route["distance_m"])
            straight_distance_m = float(route["straight_distance_m"])
            names = route["road_names"]
            start_snap_m = float(route["start_snap_m"])
            end_snap_m = float(route["end_snap_m"])
        except (KeyError, TypeError, ValueError) as error:
            raise MissionRouteError(f"第 {index + 1} 段规划结果缺少必要数据。") from error

        if (
            not isinstance(raw_points, list)
            or len(raw_points) < 2
            or any(not _valid_coordinate(point) for point in raw_points)
            or not all(math.isfinite(value) and value >= 0 for value in (
                distance_m, straight_distance_m, start_snap_m, end_snap_m
            ))
            or not isinstance(names, list)
            or any(not isinstance(name, str) for name in names)
        ):
            raise MissionRouteError(f"第 {index + 1} 段规划结果无效。")

        start_point = _endpoint(start)
        end_point = _endpoint(end)
        if (
            straight_distance_m <= 0
            or distance_m <= 0
            or straight_distance_m > distance_m + 1  # Account for planner rounding.
            or _coordinate_distance_m(raw_points[0], start_point) > 0.1
            or _coordinate_distance_m(raw_points[-1], end_point) > 0.1
        ):
            raise MissionRouteError(f"第 {index + 1} 段规划结果未连接所选主点。")

        points = [list(point) for point in raw_points]
        points[0] = start_point
        points[-1] = end_point
        if not coordinates:
            coordinates.extend(points)
            anchor_indices.append(0)
            start_index = 0
        else:
            start_index = len(coordinates) - 1
            coordinates.extend(points[1:])
        end_index = len(coordinates) - 1
        anchor_indices.append(end_index)

        if len(coordinates) > MAX_MISSION_VERTICES:
            raise MissionRouteLimitError(f"任务路线超过 {MAX_MISSION_VERTICES} 个折线点，请拆分任务。")

        total_distance_m += distance_m
        total_straight_m += straight_distance_m
        for name in names:
            if name not in road_names:
                road_names.append(name)
        segments.append({
            "id": f"{start['id']}->{end['id']}",
            "index": index,
            "from_id": start["id"],
            "to_id": end["id"],
            "start_index": start_index,
            "end_index": end_index,
            "distance_m": distance_m,
            "distance_km": round(distance_m / 1000, 3),
            "straight_distance_m": straight_distance_m,
            "estimated_minutes": round(distance_m / 1000 / speed_kmh * 60, 1),
            "start_snap_m": start_snap_m,
            "end_snap_m": end_snap_m,
            "road_names": names,
        })

    return {
        "geometry": {"type": "LineString", "coordinates": coordinates},
        "distance_m": round(total_distance_m, 1),
        "straight_distance_m": round(total_straight_m, 1),
        "start_snap_m": segments[0]["start_snap_m"],
        "end_snap_m": segments[-1]["end_snap_m"],
        "road_names": road_names,
        "building_clearance_m": routes[0].get("building_clearance_m"),
        "data_source": routes[0].get("data_source"),
        "anchors": list(anchors),
        "anchor_indices": anchor_indices,
        "segments": segments,
    }


def _coordinate_distance_m(a: list[float] | tuple[float, float], b: list[float]) -> float:
    """Metres between GeoJSON [longitude, latitude] coordinates."""
    return straight_distance_m(a[1], a[0], b[1], b[0])

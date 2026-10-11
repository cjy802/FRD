"""Generate a conservative DJI WPML waypoint KMZ draft.

The route geometry in this project is a road reference line. The generated
file is therefore intentionally labelled as a draft that must be reviewed in
DJI Pilot 2 before any operation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from math import cos, isfinite, radians, sqrt
from zipfile import ZIP_DEFLATED, ZipFile
from typing import Literal


WPML_NAMESPACE = "http://www.dji.com/wpmz/1.0.2"
DRONE_PROFILES = {
    "m30": {"enum": 67, "sub_enum": 0, "payload": 52},
    "m3e": {"enum": 77, "sub_enum": 0, "payload": 66},
}

# Road geometry contains many short OSM/OSRM segments. DJI renders every
# waypoint marker and altitude label, so exporting every vertex makes even a
# valid route unreadable. These values keep meaningful turns while removing
# sub-meter duplicates and small map-digitizing wiggles.
SIMPLIFY_TOLERANCE_M = 3.0
MIN_WAYPOINT_DISTANCE_M = 3.0
MAX_EXPORT_WAYPOINTS = 500


class KmzExportError(ValueError):
    pass


def _number(value: float, digits: int = 3) -> str:
    return f"{value:.{digits}f}".rstrip("0").rstrip(".")


def _local_xy(point: tuple[float, float], origin_latitude: float) -> tuple[float, float]:
    """Project a nearby lon/lat pair to metres for route simplification."""
    longitude, latitude = point
    metres_per_degree = 111_320.0
    return (
        longitude * metres_per_degree * cos(radians(origin_latitude)),
        latitude * metres_per_degree,
    )


def _distance_m(first: tuple[float, float], second: tuple[float, float]) -> float:
    origin_latitude = (first[1] + second[1]) / 2
    first_x, first_y = _local_xy(first, origin_latitude)
    second_x, second_y = _local_xy(second, origin_latitude)
    return sqrt((second_x - first_x) ** 2 + (second_y - first_y) ** 2)


def _point_line_distance(point: tuple[float, float], start: tuple[float, float], end: tuple[float, float]) -> float:
    origin_latitude = (start[1] + end[1] + point[1]) / 3
    point_x, point_y = _local_xy(point, origin_latitude)
    start_x, start_y = _local_xy(start, origin_latitude)
    end_x, end_y = _local_xy(end, origin_latitude)
    dx, dy = end_x - start_x, end_y - start_y
    if dx == 0 and dy == 0:
        return sqrt((point_x - start_x) ** 2 + (point_y - start_y) ** 2)
    projection = ((point_x - start_x) * dx + (point_y - start_y) * dy) / (dx * dx + dy * dy)
    projection = max(0.0, min(1.0, projection))
    closest_x = start_x + projection * dx
    closest_y = start_y + projection * dy
    return sqrt((point_x - closest_x) ** 2 + (point_y - closest_y) ** 2)


def _rdp_indices(coordinates: list[tuple[float, float]], tolerance_m: float) -> list[int]:
    """Return Ramer-Douglas-Peucker indices without recursive stack growth."""
    if len(coordinates) <= 2:
        return list(range(len(coordinates)))
    kept = {0, len(coordinates) - 1}
    pending = [(0, len(coordinates) - 1)]
    while pending:
        start_index, end_index = pending.pop()
        start, end = coordinates[start_index], coordinates[end_index]
        farthest_index = -1
        farthest_distance = tolerance_m
        for index in range(start_index + 1, end_index):
            distance = _point_line_distance(coordinates[index], start, end)
            if distance > farthest_distance:
                farthest_index = index
                farthest_distance = distance
        if farthest_index >= 0:
            kept.add(farthest_index)
            pending.append((start_index, farthest_index))
            pending.append((farthest_index, end_index))
    return sorted(kept)


def _simplify_coordinates(coordinates: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Reduce dense road geometry while preserving route order and endpoints."""
    if len(coordinates) <= 2:
        return coordinates

    filtered = [coordinates[0]]
    for point in coordinates[1:-1]:
        if _distance_m(filtered[-1], point) >= MIN_WAYPOINT_DISTANCE_M:
            filtered.append(point)
    if _distance_m(filtered[-1], coordinates[-1]) < MIN_WAYPOINT_DISTANCE_M:
        filtered[-1] = coordinates[-1]
    else:
        filtered.append(coordinates[-1])

    tolerance = SIMPLIFY_TOLERANCE_M
    indices = _rdp_indices(filtered, tolerance)
    while len(indices) > MAX_EXPORT_WAYPOINTS:
        tolerance *= 1.5
        indices = _rdp_indices(filtered, tolerance)
    return [filtered[index] for index in indices]


def _mission_config(speed_m_s: float, rth_height_m: float, drone: dict[str, int]) -> str:
    return f"""<wpml:missionConfig>
  <wpml:flyToWaylineMode>safely</wpml:flyToWaylineMode>
  <wpml:finishAction>goHome</wpml:finishAction>
  <wpml:exitOnRCLost>executeLostAction</wpml:exitOnRCLost>
  <wpml:executeRCLostAction>hover</wpml:executeRCLostAction>
  <wpml:takeOffSecurityHeight>20</wpml:takeOffSecurityHeight>
  <wpml:globalTransitionalSpeed>{_number(speed_m_s)}</wpml:globalTransitionalSpeed>
  <wpml:globalRTHHeight>{_number(rth_height_m)}</wpml:globalRTHHeight>
  <wpml:droneInfo>
    <wpml:droneEnumValue>{drone['enum']}</wpml:droneEnumValue>
    <wpml:droneSubEnumValue>{drone['sub_enum']}</wpml:droneSubEnumValue>
  </wpml:droneInfo>
  <wpml:payloadInfo>
    <wpml:payloadEnumValue>{drone['payload']}</wpml:payloadEnumValue>
    <wpml:payloadPositionIndex>0</wpml:payloadPositionIndex>
  </wpml:payloadInfo>
</wpml:missionConfig>"""


def _template_waypoint(index: int, longitude: float, latitude: float, altitude_m: float) -> str:
    return f"""<Placemark>
  <Point><coordinates>{_number(longitude, 7)},{_number(latitude, 7)}</coordinates></Point>
  <wpml:index>{index}</wpml:index>
  <wpml:ellipsoidHeight>{_number(altitude_m)}</wpml:ellipsoidHeight>
  <wpml:height>{_number(altitude_m)}</wpml:height>
  <wpml:useGlobalHeight>1</wpml:useGlobalHeight>
  <wpml:useGlobalSpeed>1</wpml:useGlobalSpeed>
  <wpml:useGlobalHeadingParam>1</wpml:useGlobalHeadingParam>
  <wpml:useGlobalTurnParam>1</wpml:useGlobalTurnParam>
</Placemark>"""


def _wayline_waypoint(index: int, longitude: float, latitude: float, altitude_m: float, speed_m_s: float) -> str:
    return f"""<Placemark>
  <Point><coordinates>{_number(longitude, 7)},{_number(latitude, 7)}</coordinates></Point>
  <wpml:index>{index}</wpml:index>
  <wpml:executeHeight>{_number(altitude_m)}</wpml:executeHeight>
  <wpml:waypointSpeed>{_number(speed_m_s)}</wpml:waypointSpeed>
  <wpml:waypointHeadingParam><wpml:waypointHeadingMode>followWayline</wpml:waypointHeadingMode></wpml:waypointHeadingParam>
  <wpml:waypointTurnParam>
    <wpml:waypointTurnMode>toPointAndStopWithDiscontinuityCurvature</wpml:waypointTurnMode>
    <wpml:waypointTurnDampingDist>0</wpml:waypointTurnDampingDist>
  </wpml:waypointTurnParam>
  <wpml:useStraightLine>1</wpml:useStraightLine>
</Placemark>"""


def _template_kml(coordinates: list[tuple[float, float]], altitude_m: float, speed_m_s: float, rth_height_m: float, timestamp_ms: int, drone: dict[str, int]) -> str:
    placemarks = "\n".join(_template_waypoint(i, lon, lat, altitude_m) for i, (lon, lat) in enumerate(coordinates))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:wpml="{WPML_NAMESPACE}">
  <Document>
    <wpml:author>Low Altitude Flight Decision Workbench</wpml:author>
    <wpml:createTime>{timestamp_ms}</wpml:createTime>
    <wpml:updateTime>{timestamp_ms}</wpml:updateTime>
    {_mission_config(speed_m_s, rth_height_m, drone)}
    <Folder>
      <wpml:templateType>waypoint</wpml:templateType>
      <wpml:templateId>0</wpml:templateId>
      <wpml:waylineCoordinateSysParam>
        <wpml:coordinateMode>WGS84</wpml:coordinateMode>
        <wpml:heightMode>relativeToStartPoint</wpml:heightMode>
        <wpml:positioningType>GPS</wpml:positioningType>
      </wpml:waylineCoordinateSysParam>
      <wpml:globalHeight>{_number(altitude_m)}</wpml:globalHeight>
      <wpml:autoFlightSpeed>{_number(speed_m_s)}</wpml:autoFlightSpeed>
      <wpml:gimbalPitchMode>manual</wpml:gimbalPitchMode>
      <wpml:globalWaypointHeadingParam><wpml:waypointHeadingMode>followWayline</wpml:waypointHeadingMode></wpml:globalWaypointHeadingParam>
      <wpml:globalWaypointTurnMode>toPointAndStopWithDiscontinuityCurvature</wpml:globalWaypointTurnMode>
      <wpml:globalUseStraightLine>1</wpml:globalUseStraightLine>
      {placemarks}
    </Folder>
  </Document>
</kml>
"""


def _waylines_wpml(coordinates: list[tuple[float, float]], altitude_m: float, speed_m_s: float, rth_height_m: float, drone: dict[str, int]) -> str:
    placemarks = "\n".join(_wayline_waypoint(i, lon, lat, altitude_m, speed_m_s) for i, (lon, lat) in enumerate(coordinates))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:wpml="{WPML_NAMESPACE}">
  <Document>
    {_mission_config(speed_m_s, rth_height_m, drone)}
    <Folder>
      <wpml:templateId>0</wpml:templateId>
      <wpml:executeHeightMode>relativeToStartPoint</wpml:executeHeightMode>
      <wpml:waylineId>0</wpml:waylineId>
      <wpml:autoFlightSpeed>{_number(speed_m_s)}</wpml:autoFlightSpeed>
      {placemarks}
    </Folder>
  </Document>
</kml>
"""


def build_dji_kmz(coordinates, altitude_m: float, speed_kmh: float, drone_model: Literal["m30", "m3e"] = "m30") -> bytes:
    if not isinstance(coordinates, list) or len(coordinates) < 2 or len(coordinates) > 65535:
        raise KmzExportError("DJI 航点草稿至少需要 2 个、最多 65535 个路线点。")
    if not isfinite(altitude_m) or not 0 < altitude_m <= 1500:
        raise KmzExportError("DJI 航点草稿的相对起飞点高度须在 0–1500 米。")
    if not isfinite(speed_kmh) or not 3.6 <= speed_kmh <= 54:
        raise KmzExportError("DJI WPML 草稿速度须在 3.6–54 km/h（1–15 m/s）。")
    if drone_model not in DRONE_PROFILES:
        raise KmzExportError("暂不支持该 DJI 机型模板。")
    cleaned: list[tuple[float, float]] = []
    for point in coordinates:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            raise KmzExportError("路线点坐标格式无效。")
        longitude, latitude = float(point[0]), float(point[1])
        if not (-180 <= longitude <= 180 and -90 <= latitude <= 90 and isfinite(longitude) and isfinite(latitude)):
            raise KmzExportError("路线包含超出经纬度范围的坐标。")
        if not cleaned or (longitude, latitude) != cleaned[-1]:
            cleaned.append((longitude, latitude))
    if len(cleaned) < 2:
        raise KmzExportError("路线去重后不足 2 个航点。")
    cleaned = _simplify_coordinates(cleaned)
    if len(cleaned) < 2:
        raise KmzExportError("路线抽稀后不足 2 个航点。")
    speed_m_s = speed_kmh / 3.6
    rth_height_m = max(20.0, altitude_m)
    drone = DRONE_PROFILES[drone_model]
    timestamp_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("wpmz/template.kml", _template_kml(cleaned, altitude_m, speed_m_s, rth_height_m, timestamp_ms, drone))
        archive.writestr("wpmz/waylines.wpml", _waylines_wpml(cleaned, altitude_m, speed_m_s, rth_height_m, drone))
    return output.getvalue()

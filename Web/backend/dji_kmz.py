"""Generate a conservative DJI WPML waypoint KMZ draft.

The route geometry in this project is a road reference line. The generated
file is therefore intentionally labelled as a draft that must be reviewed in
DJI Fly or DJI Pilot 2 before any operation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from math import cos, isfinite, radians, sqrt
from zipfile import ZIP_DEFLATED, ZipFile
from typing import Literal


PILOT2_NAMESPACE = "http://www.dji.com/wpmz/1.0.2"
FLY_NAMESPACE = "http://www.uav.com/wpmz/1.0.2"
DRONE_PROFILES = {
    "m30": {"namespace": PILOT2_NAMESPACE, "author": "Low Altitude Flight Decision Workbench", "enum": 67, "sub_enum": 0, "payload": 52, "fly": False},
    "m3e": {"namespace": PILOT2_NAMESPACE, "author": "Low Altitude Flight Decision Workbench", "enum": 77, "sub_enum": 0, "payload": 66, "fly": False},
    # DJI Fly on RC 2 uses a consumer WPML namespace and does not include
    # enterprise payloadInfo. Air 3/Air 3S use the same consumer waypoint
    # profile as the RC 2 waypoint files currently available to this project.
    "fly": {"namespace": FLY_NAMESPACE, "author": "fly", "enum": 68, "sub_enum": 0, "payload": None, "fly": True},
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


def _simplify_coordinate_indices(coordinates: list[tuple[float, float]], protected_indices: set[int] | None = None) -> list[int]:
    """Return retained original indices after reducing dense road geometry."""
    if len(coordinates) <= 2:
        return list(range(len(coordinates)))

    protected_indices = protected_indices or set()
    filtered = [coordinates[0]]
    filtered_indices = [0]
    for index, point in enumerate(coordinates[1:-1], start=1):
        if _distance_m(filtered[-1], point) >= MIN_WAYPOINT_DISTANCE_M or index in protected_indices:
            filtered.append(point)
            filtered_indices.append(index)
    if _distance_m(filtered[-1], coordinates[-1]) < MIN_WAYPOINT_DISTANCE_M:
        filtered[-1] = coordinates[-1]
        filtered_indices[-1] = len(coordinates) - 1
    else:
        filtered.append(coordinates[-1])
        filtered_indices.append(len(coordinates) - 1)

    tolerance = SIMPLIFY_TOLERANCE_M
    indices = _rdp_indices(filtered, tolerance)
    protected_filtered = {filtered_indices.index(index) for index in protected_indices if index in filtered_indices}
    indices = sorted(set(indices) | protected_filtered)
    while len(indices) > MAX_EXPORT_WAYPOINTS:
        tolerance *= 1.5
        indices = sorted(set(_rdp_indices(filtered, tolerance)) | protected_filtered)
    return [filtered_indices[index] for index in indices]


def _simplify_coordinates(coordinates: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Reduce dense road geometry while preserving route order and endpoints."""
    return [coordinates[index] for index in _simplify_coordinate_indices(coordinates)]


def _mission_config(speed_m_s: float, rth_height_m: float, drone: dict[str, object]) -> str:
    payload = ""
    if drone["payload"] is not None:
        payload = f"""  <wpml:payloadInfo>
    <wpml:payloadEnumValue>{drone['payload']}</wpml:payloadEnumValue>
    <wpml:payloadPositionIndex>0</wpml:payloadPositionIndex>
  </wpml:payloadInfo>
"""
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
{payload}</wpml:missionConfig>"""


HEADING_MODES = {"followWayline", "manually", "fixed", "smoothTransition", "towardPOI"}


def _heading_param(setting: dict[str, object] | None = None) -> str:
    setting = setting or {}
    mode = str(setting.get("heading_mode", "followWayline"))
    angle = float(setting.get("heading_angle", 0))
    poi = setting.get("poi")
    if mode not in HEADING_MODES:
        mode = "followWayline"
    if not isfinite(angle) or not -180 <= angle <= 180:
        angle = 0
    if isinstance(poi, (list, tuple)) and len(poi) >= 2:
        poi_text = f"{_number(float(poi[0]), 7)},{_number(float(poi[1]), 7)},{_number(float(poi[2]) if len(poi) > 2 else 0)}"
    else:
        poi_text = "0.000000,0.000000,0.000000"
    if mode != "towardPOI":
        poi_text = "0.000000,0.000000,0.000000"
    return """  <wpml:waypointHeadingParam>
    <wpml:waypointHeadingMode>{mode}</wpml:waypointHeadingMode>
    <wpml:waypointHeadingAngle>{angle}</wpml:waypointHeadingAngle>
    <wpml:waypointPoiPoint>{poi_text}</wpml:waypointPoiPoint>
    <wpml:waypointHeadingPathMode>followBadArc</wpml:waypointHeadingPathMode>
  </wpml:waypointHeadingParam>""".format(mode=mode, angle=_number(angle), poi_text=poi_text)


def _template_waypoint(index: int, longitude: float, latitude: float, altitude_m: float, speed_m_s: float, fly_profile: bool, setting: dict[str, object] | None = None) -> str:
    setting = setting or {}
    raw_altitude = setting.get("altitude_m")
    point_altitude = altitude_m if raw_altitude is None else float(raw_altitude)
    gimbal_pitch = float(setting.get("gimbal_pitch_angle", 0))
    if fly_profile:
        settings = f"""  <wpml:useGlobalHeight>0</wpml:useGlobalHeight>
  <wpml:useGlobalSpeed>0</wpml:useGlobalSpeed>
{_heading_param(setting)}
  <wpml:useGlobalHeadingParam>0</wpml:useGlobalHeadingParam>
  <wpml:useGlobalTurnParam>0</wpml:useGlobalTurnParam>
  <wpml:waypointSpeed>{_number(speed_m_s)}</wpml:waypointSpeed>
  <wpml:waypointTurnParam>
    <wpml:waypointTurnMode>toPointAndStopWithDiscontinuityCurvature</wpml:waypointTurnMode>
    <wpml:waypointTurnDampingDist>0</wpml:waypointTurnDampingDist>
  </wpml:waypointTurnParam>
  <wpml:gimbalPitchAngle>{_number(gimbal_pitch)}</wpml:gimbalPitchAngle>"""
    else:
        settings = f"""  <wpml:useGlobalHeight>0</wpml:useGlobalHeight>
  <wpml:useGlobalSpeed>0</wpml:useGlobalSpeed>
{_heading_param(setting)}
  <wpml:useGlobalHeadingParam>0</wpml:useGlobalHeadingParam>
  <wpml:useGlobalTurnParam>0</wpml:useGlobalTurnParam>
  <wpml:waypointSpeed>{_number(speed_m_s)}</wpml:waypointSpeed>
  <wpml:waypointTurnParam>
    <wpml:waypointTurnMode>toPointAndStopWithDiscontinuityCurvature</wpml:waypointTurnMode>
    <wpml:waypointTurnDampingDist>0</wpml:waypointTurnDampingDist>
  </wpml:waypointTurnParam>
  <wpml:gimbalPitchAngle>{_number(gimbal_pitch)}</wpml:gimbalPitchAngle>"""
    return f"""<Placemark>
  <Point><coordinates>{_number(longitude, 7)},{_number(latitude, 7)}</coordinates></Point>
  <wpml:index>{index}</wpml:index>
  <wpml:ellipsoidHeight>{_number(point_altitude)}</wpml:ellipsoidHeight>
  <wpml:height>{_number(point_altitude)}</wpml:height>
{settings}
</Placemark>"""


def _wayline_waypoint(index: int, longitude: float, latitude: float, altitude_m: float, speed_m_s: float, setting: dict[str, object] | None = None) -> str:
    setting = setting or {}
    raw_altitude = setting.get("altitude_m")
    point_altitude = altitude_m if raw_altitude is None else float(raw_altitude)
    gimbal_pitch = float(setting.get("gimbal_pitch_angle", 0))
    return f"""<Placemark>
  <Point><coordinates>{_number(longitude, 7)},{_number(latitude, 7)}</coordinates></Point>
  <wpml:index>{index}</wpml:index>
  <wpml:executeHeight>{_number(point_altitude)}</wpml:executeHeight>
  <wpml:waypointSpeed>{_number(speed_m_s)}</wpml:waypointSpeed>
{_heading_param(setting)}
  <wpml:waypointTurnParam>
    <wpml:waypointTurnMode>toPointAndStopWithDiscontinuityCurvature</wpml:waypointTurnMode>
    <wpml:waypointTurnDampingDist>0</wpml:waypointTurnDampingDist>
  </wpml:waypointTurnParam>
  <wpml:gimbalPitchAngle>{_number(gimbal_pitch)}</wpml:gimbalPitchAngle>
  <wpml:useStraightLine>1</wpml:useStraightLine>
</Placemark>"""


def _template_kml(coordinates: list[tuple[float, float]], altitude_m: float, speed_m_s: float, rth_height_m: float, timestamp_ms: int, drone: dict[str, object], waypoint_settings: list[dict[str, object]] | None = None) -> str:
    waypoint_settings = waypoint_settings or [{} for _ in coordinates]
    placemarks = "\n".join(_template_waypoint(i, lon, lat, altitude_m, speed_m_s, bool(drone["fly"]), waypoint_settings[i]) for i, (lon, lat) in enumerate(coordinates))
    global_heading = _heading_param().replace("waypointHeadingParam", "globalWaypointHeadingParam", 2) if drone["fly"] else "      <wpml:globalWaypointHeadingParam><wpml:waypointHeadingMode>followWayline</wpml:waypointHeadingMode></wpml:globalWaypointHeadingParam>"
    gimbal_mode = "usePointSetting"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:wpml="{drone['namespace']}">
  <Document>
    <wpml:author>{drone['author']}</wpml:author>
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
      <wpml:gimbalPitchMode>{gimbal_mode}</wpml:gimbalPitchMode>
{global_heading}
      <wpml:globalWaypointTurnMode>toPointAndStopWithDiscontinuityCurvature</wpml:globalWaypointTurnMode>
      <wpml:globalUseStraightLine>1</wpml:globalUseStraightLine>
      {placemarks}
    </Folder>
  </Document>
</kml>
"""


def _waylines_wpml(coordinates: list[tuple[float, float]], altitude_m: float, speed_m_s: float, rth_height_m: float, drone: dict[str, object], waypoint_settings: list[dict[str, object]] | None = None) -> str:
    waypoint_settings = waypoint_settings or [{} for _ in coordinates]
    placemarks = "\n".join(_wayline_waypoint(i, lon, lat, altitude_m, speed_m_s, waypoint_settings[i]) for i, (lon, lat) in enumerate(coordinates))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:wpml="{drone['namespace']}">
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


def _normalise_waypoint_settings(settings, count: int) -> list[dict[str, object]]:
    if settings is None:
        return [{} for _ in range(count)]
    if not isinstance(settings, list) or len(settings) != count:
        raise KmzExportError("逐点航点设置必须与原始路线点数量一致。")
    normalised: list[dict[str, object]] = []
    for item in settings:
        if item is None:
            normalised.append({})
            continue
        if not isinstance(item, dict):
            raise KmzExportError("逐点航点设置格式无效。")
        heading_mode = item.get("heading_mode", "followWayline")
        if heading_mode not in HEADING_MODES:
            raise KmzExportError("航点机头方向模式无效。")
        altitude = item.get("altitude_m")
        if altitude is not None and (not isfinite(float(altitude)) or not 0 < float(altitude) <= 1500):
            raise KmzExportError("航点高度必须在 0–1500 米。")
        pitch = item.get("gimbal_pitch_angle", 0)
        if not isfinite(float(pitch)) or not -90 <= float(pitch) <= 45:
            raise KmzExportError("摄像头俯仰角必须在 -90–45 度。")
        angle = item.get("heading_angle", 0)
        if not isfinite(float(angle)) or not -180 <= float(angle) <= 180:
            raise KmzExportError("航点机头角度必须在 -180–180 度。")
        normalised.append({
            "altitude_m": float(altitude) if altitude is not None else None,
            "heading_mode": heading_mode,
            "heading_angle": float(angle),
            "gimbal_pitch_angle": float(pitch),
            "poi": item.get("poi"),
        })
    return normalised


def build_dji_kmz(coordinates, altitude_m: float, speed_kmh: float, drone_model: Literal["fly", "m30", "m3e"] = "fly", waypoint_settings=None) -> bytes:
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
    settings = _normalise_waypoint_settings(waypoint_settings, len(cleaned))
    protected_indices = {
        index for index, setting in enumerate(settings)
        if setting.get("altitude_m") is not None and float(setting["altitude_m"]) != altitude_m
        or setting.get("heading_mode") not in (None, "followWayline")
        or float(setting.get("heading_angle", 0)) != 0
        or float(setting.get("gimbal_pitch_angle", 0)) != 0
    }
    retained_indices = _simplify_coordinate_indices(cleaned, protected_indices)
    cleaned = [cleaned[index] for index in retained_indices]
    settings = [settings[index] for index in retained_indices]
    if len(cleaned) < 2:
        raise KmzExportError("路线抽稀后不足 2 个航点。")
    speed_m_s = speed_kmh / 3.6
    rth_height_m = max(20.0, altitude_m)
    drone = DRONE_PROFILES[drone_model]
    timestamp_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("wpmz/template.kml", _template_kml(cleaned, altitude_m, speed_m_s, rth_height_m, timestamp_ms, drone, settings))
        archive.writestr("wpmz/waylines.wpml", _waylines_wpml(cleaned, altitude_m, speed_m_s, rth_height_m, drone, settings))
    return output.getvalue()

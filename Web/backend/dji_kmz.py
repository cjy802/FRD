"""Generate a conservative DJI WPML waypoint KMZ draft.

The route geometry in this project is a road reference line. The generated
file is therefore intentionally labelled as a draft that must be reviewed in
DJI Fly or DJI Pilot 2 before any operation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from math import atan2, cos, degrees, isfinite, radians, sin, sqrt
from zipfile import ZIP_DEFLATED, ZipFile
from typing import Literal


PILOT2_NAMESPACE = "http://www.dji.com/wpmz/1.0.2"
# DJI 官方 WPML 规范的命名空间是 dji.com/wpmz；新版 DJI Fly 自身也写出过
# uav.com 变体，但按官方文档生成兼容性最好。
FLY_NAMESPACE = PILOT2_NAMESPACE
DRONE_PROFILES = {
    "m30": {"namespace": PILOT2_NAMESPACE, "author": "Low Altitude Flight Decision Workbench", "enum": 67, "sub_enum": 0, "payload": 52, "fly": False},
    "m3e": {"namespace": PILOT2_NAMESPACE, "author": "Low Altitude Flight Decision Workbench", "enum": 77, "sub_enum": 0, "payload": 66, "fly": False},
    # DJI Fly on RC 2 uses a consumer WPML namespace and does not include
    # enterprise payloadInfo. Air 3/Air 3S use the same consumer waypoint
    # profile as the RC 2 waypoint files currently available to this project.
    "fly": {"namespace": FLY_NAMESPACE, "author": "fly", "enum": 68, "sub_enum": 0, "payload": None, "fly": True},
}

# WPML 规范的 waypointTurnMode 只接受 coordinateTurn / toPointAndStop 两个
# 枚举值；DJI Fly 遇到非法值时按默认平滑转弯绘制，导入的航线会显示成
# 弯曲交叉的乱线。
WAYPOINT_TURN_MODE = "coordinateTurn"

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
    if _distance_m(filtered[-1], coordinates[-1]) < MIN_WAYPOINT_DISTANCE_M and filtered_indices[-1] not in protected_indices:
        filtered[-1] = coordinates[-1]
        filtered_indices[-1] = len(coordinates) - 1
    else:
        filtered.append(coordinates[-1])
        filtered_indices.append(len(coordinates) - 1)

    tolerance = SIMPLIFY_TOLERANCE_M
    indices = _rdp_indices(filtered, tolerance)
    filtered_position = {original: position for position, original in enumerate(filtered_indices)}
    protected_filtered = {filtered_position[index] for index in protected_indices if index in filtered_position}
    if len(protected_filtered | {0, len(filtered) - 1}) > MAX_EXPORT_WAYPOINTS:
        raise KmzExportError(f"已编辑航点超过 {MAX_EXPORT_WAYPOINTS} 个，无法在保留全部设置的同时导出。")
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
    # WPML fixed holds the existing yaw; smoothTransition actually targets the supplied angle.
    if mode == "fixed":
        mode = "smoothTransition"
    if not isfinite(angle):
        angle = 0
    angle = ((angle + 180) % 360) - 180
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


def _template_waypoint(index: int, longitude: float, latitude: float, altitude_m: float, speed_m_s: float, setting: dict[str, object] | None = None) -> str:
    setting = setting or {}
    raw_altitude = setting.get("altitude_m")
    point_altitude = altitude_m if raw_altitude is None else float(raw_altitude)
    gimbal_pitch = float(setting.get("gimbal_pitch_angle", 0))
    settings = f"""  <wpml:useGlobalHeight>0</wpml:useGlobalHeight>
  <wpml:useGlobalSpeed>0</wpml:useGlobalSpeed>
{_heading_param(setting)}
  <wpml:useGlobalHeadingParam>0</wpml:useGlobalHeadingParam>
  <wpml:useGlobalTurnParam>0</wpml:useGlobalTurnParam>
  <wpml:waypointSpeed>{_number(speed_m_s)}</wpml:waypointSpeed>
  <wpml:waypointTurnParam>
    <wpml:waypointTurnMode>{WAYPOINT_TURN_MODE}</wpml:waypointTurnMode>
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
    <wpml:waypointTurnMode>{WAYPOINT_TURN_MODE}</wpml:waypointTurnMode>
    <wpml:waypointTurnDampingDist>0</wpml:waypointTurnDampingDist>
  </wpml:waypointTurnParam>
  <wpml:gimbalPitchAngle>{_number(gimbal_pitch)}</wpml:gimbalPitchAngle>
  <wpml:useStraightLine>1</wpml:useStraightLine>
</Placemark>"""


def _template_kml(coordinates: list[tuple[float, float]], altitude_m: float, speed_m_s: float, rth_height_m: float, timestamp_ms: int, drone: dict[str, object], waypoint_settings: list[dict[str, object]] | None = None) -> str:
    waypoint_settings = waypoint_settings or [{} for _ in coordinates]
    placemarks = "\n".join(_template_waypoint(i, lon, lat, altitude_m, speed_m_s, waypoint_settings[i]) for i, (lon, lat) in enumerate(coordinates))
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
      <wpml:globalWaypointTurnMode>{WAYPOINT_TURN_MODE}</wpml:globalWaypointTurnMode>
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
        if heading_mode == "manually":
            raise KmzExportError("手动控制无法写成自动朝向；请改为沿航线或指定角度后导出。")
        if heading_mode == "towardPOI" and not item.get("poi"):
            raise KmzExportError("朝向兴趣点需要提供有效的兴趣点坐标。")
        altitude = item.get("altitude_m")
        if altitude is not None and (not isfinite(float(altitude)) or not 0 < float(altitude) <= 1500):
            raise KmzExportError("航点高度必须在 0–1500 米。")
        pitch = item.get("gimbal_pitch_angle", 0)
        if not isfinite(float(pitch)) or not -90 <= float(pitch) <= 45:
            raise KmzExportError("摄像头俯仰角必须在 -90–45 度。")
        angle = item.get("heading_angle", 0)
        if not isfinite(float(angle)):
            raise KmzExportError("航点机头角度必须是有限数值。")
        normalised.append({
            "altitude_m": float(altitude) if altitude is not None else None,
            "heading_mode": heading_mode,
            "heading_angle": float(angle),
            "gimbal_pitch_angle": float(pitch),
            "export_anchor": item.get("export_anchor", False),
            "poi": item.get("poi"),
        })
    return normalised


def _has_custom_setting(setting: dict[str, object], altitude_m: float) -> bool:
    return bool(setting.get("export_anchor")) or (
        setting.get("altitude_m") is not None and float(setting["altitude_m"]) != altitude_m
        or setting.get("heading_mode") not in (None, "followWayline")
        or float(setting.get("heading_angle", 0)) != 0
        or float(setting.get("gimbal_pitch_angle", 0)) != 0
    )


def _setting_key(setting: dict[str, object], altitude_m: float) -> tuple[object, ...]:
    """Compare the effective output settings, ignoring unset default values."""
    heading_mode = setting.get("heading_mode", "followWayline")
    poi = setting.get("poi")
    return (
        altitude_m if setting.get("altitude_m") is None else setting["altitude_m"],
        heading_mode,
        setting.get("heading_angle", 0) if heading_mode != "followWayline" else None,
        setting.get("gimbal_pitch_angle", 0),
        tuple(poi) if heading_mode == "towardPOI" and isinstance(poi, (list, tuple)) else None,
    )


def _protected_setting_indices(settings: list[dict[str, object]], altitude_m: float) -> set[int]:
    """Keep explicit edits and both sides of every settings transition."""
    protected = {index for index, setting in enumerate(settings) if setting.get("export_anchor")}
    keys = [_setting_key(setting, altitude_m) for setting in settings]
    for index in range(1, len(keys)):
        if keys[index] != keys[index - 1]:
            protected.update((index - 1, index))
    return protected


def _course_heading(coordinates: list[tuple[float, float]], index: int) -> float:
    """Keep unused heading metadata aligned with the course for external viewers."""
    first, second = (coordinates[index], coordinates[index + 1]) if index < len(coordinates) - 1 else (coordinates[index - 1], coordinates[index])
    latitude1, latitude2 = radians(first[1]), radians(second[1])
    delta_longitude = radians(second[0] - first[0])
    bearing = degrees(atan2(
        sin(delta_longitude) * cos(latitude2),
        cos(latitude1) * sin(latitude2) - sin(latitude1) * cos(latitude2) * cos(delta_longitude),
    ))
    return ((bearing + 180) % 360) - 180


def build_dji_kmz(coordinates, altitude_m: float, speed_kmh: float, drone_model: Literal["fly", "m30", "m3e"] = "fly", waypoint_settings=None) -> bytes:
    if not isinstance(coordinates, list) or len(coordinates) < 2 or len(coordinates) > 65535:
        raise KmzExportError("DJI 航点草稿至少需要 2 个、最多 65535 个路线点。")
    if not isfinite(altitude_m) or not 0 < altitude_m <= 1500:
        raise KmzExportError("DJI 航点草稿的相对起飞点高度须在 0–1500 米。")
    if not isfinite(speed_kmh) or not 3.6 <= speed_kmh <= 54:
        raise KmzExportError("DJI WPML 草稿速度须在 3.6–54 km/h（1–15 m/s）。")
    if drone_model not in DRONE_PROFILES:
        raise KmzExportError("暂不支持该 DJI 机型模板。")
    settings = _normalise_waypoint_settings(waypoint_settings, len(coordinates))
    cleaned: list[tuple[float, float]] = []
    cleaned_settings: list[dict[str, object]] = []
    for point, setting in zip(coordinates, settings):
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            raise KmzExportError("路线点坐标格式无效。")
        longitude, latitude = float(point[0]), float(point[1])
        if not (-180 <= longitude <= 180 and -90 <= latitude <= 90 and isfinite(longitude) and isfinite(latitude)):
            raise KmzExportError("路线包含超出经纬度范围的坐标。")
        point = (longitude, latitude)
        if cleaned and _distance_m(cleaned[-1], point) < 0.5:
            # 近重复点合并为一个；完全重合、或两端都带自定义设置时是真正的冲突。
            if _has_custom_setting(setting, altitude_m):
                if (longitude, latitude) == cleaned[-1] or _has_custom_setting(cleaned_settings[-1], altitude_m):
                    raise KmzExportError("已编辑航点与前一个航点位置重合；请移动或删除重复点后导出。")
                cleaned[-1] = point
                cleaned_settings[-1] = setting
            continue
        cleaned.append(point)
        cleaned_settings.append(setting)
    if len(cleaned) < 2:
        raise KmzExportError("路线去重后不足 2 个航点。")
    protected_indices = _protected_setting_indices(cleaned_settings, altitude_m)
    retained_indices = _simplify_coordinate_indices(cleaned, protected_indices)
    cleaned = [cleaned[index] for index in retained_indices]
    settings = [cleaned_settings[index] for index in retained_indices]
    if len(cleaned) < 2:
        raise KmzExportError("路线抽稀后不足 2 个航点。")
    for index, setting in enumerate(settings):
        if setting.get("heading_mode", "followWayline") == "followWayline":
            # DJI follows the course in this mode. Avoid a misleading 0-degree
            # target in viewers that also interpolate waypointHeadingAngle.
            settings[index] = {**setting, "heading_angle": _course_heading(cleaned, index)}
    speed_m_s = speed_kmh / 3.6
    rth_height_m = max(20.0, altitude_m)
    drone = DRONE_PROFILES[drone_model]
    timestamp_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("wpmz/template.kml", _template_kml(cleaned, altitude_m, speed_m_s, rth_height_m, timestamp_ms, drone, settings))
        archive.writestr("wpmz/waylines.wpml", _waylines_wpml(cleaned, altitude_m, speed_m_s, rth_height_m, drone, settings))
    return output.getvalue()

"""Local DJI video + SRT review sessions for locating follow-up photos.

The video is stored for browser playback only. This module parses the SRT
telemetry and never runs image inference on video frames.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from math import ceil, cos, radians
from pathlib import Path
from typing import Any

from fastapi import UploadFile

BACKEND_ROOT = Path(__file__).resolve().parent
DEFAULT_STORAGE = BACKEND_ROOT / "user_data" / "video_surveys"
MAX_VIDEO_BYTES = 8 * 1024 * 1024 * 1024
MAX_SRT_BYTES = 32 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
ALLOWED_VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v"}
_TIMESTAMP = re.compile(r"(?P<h>\d{2,}):(?P<m>\d{2}):(?P<s>\d{2})[,.](?P<ms>\d{1,3})")
# DJI writes the absolute device-local clock into every cue block, e.g.
# "2023-10-23. 10:30:57,600" (legacy) or "2026-10-23 10:30:45.000" (newer Fly).
_RECORDING_DATETIME = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[. ]\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})")
_BLOCK_SEPARATOR = re.compile(r"\n\s*\n")
_JOB_ID = re.compile(r"\d{14}-[0-9a-f]{8}")


class VideoSurveyError(Exception):
    def __init__(self, message: str, http_status: int = 400) -> None:
        super().__init__(message)
        self.http_status = http_status


def _parse_timestamp(value: str) -> float | None:
    match = _TIMESTAMP.search(value)
    if not match:
        return None
    milliseconds = (match.group("ms") + "000")[:3]
    return (int(match.group("h")) * 3600 + int(match.group("m")) * 60
            + int(match.group("s")) + int(milliseconds) / 1000)


def _field_value(payload: str, names: tuple[str, ...]) -> str | None:
    alternatives = "|".join(re.escape(name).replace(r"\ ", r"\s+") for name in names)
    match = re.search(rf"(?:\[|\b)(?:{alternatives})\s*[:=]\s*([^\]\r\n]+)", payload, re.IGNORECASE)
    return match.group(1).strip() if match else None


def _number(value: str | None) -> float | None:
    if value is None:
        return None
    match = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", value)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _number_list(value: str | None) -> list[float]:
    if value is None:
        return []
    values = []
    for item in re.findall(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", value):
        try:
            values.append(float(item))
        except ValueError:
            continue
    return values


def parse_srt_telemetry(text: str) -> list[dict[str, Any]]:
    """Extract cue times and common DJI bracketed GPS/altitude fields."""
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    points: list[dict[str, Any]] = []
    for block in _BLOCK_SEPARATOR.split(text):
        lines = block.splitlines()
        time_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if time_index is None:
            continue
        start_text = lines[time_index].split("-->", 1)[0].strip()
        time_seconds = _parse_timestamp(start_text)
        if time_seconds is None:
            continue
        payload = " ".join(lines[time_index + 1:])
        latitude = _number(_field_value(payload, ("latitude", "lat")))
        longitude = _number(_field_value(payload, ("longitude", "longtitude", "lon", "lng")))
        if latitude is None or longitude is None:
            gps_values = _number_list(_field_value(payload, ("gps", "gps_info", "gps_position")))
            if len(gps_values) >= 2:
                latitude = latitude if latitude is not None else gps_values[0]
                longitude = longitude if longitude is not None else gps_values[1]
        if latitude is None or longitude is None or not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            continue

        altitude = _number(_field_value(payload, ("rel_alt", "relative_altitude", "altitude_fusion", "altitude", "height")))
        heading = _number(_field_value(payload, ("yaw", "heading", "flight_yaw")))
        gimbal_pitch = _number(_field_value(payload, ("gb_pitch", "gimbal_pitch", "gimbal_pitch_angle")))
        if heading is None:
            attitude = _number_list(_field_value(payload, ("f_pry", "f.pry", "f pry", "flight_pry")))
            if len(attitude) >= 3:
                heading = attitude[2]
        if gimbal_pitch is None:
            gimbal = _number_list(_field_value(payload, ("g_pry", "g.pry", "g.g.pry", "g pry", "gb_pry", "gimbal_pry")))
            if gimbal:
                gimbal_pitch = gimbal[0]
        points.append({
            "time_seconds": time_seconds,
            "latitude": latitude,
            "longitude": longitude,
            "altitude_m": altitude,
            "heading_deg": heading,
            "gimbal_pitch_deg": gimbal_pitch,
        })

    points.sort(key=lambda point: point["time_seconds"])
    # Keep the last telemetry record when an SRT repeats a cue timestamp.
    unique: dict[float, dict[str, Any]] = {}
    for point in points:
        unique[point["time_seconds"]] = point
    return list(unique.values())


def parse_srt_recording_window(text: str) -> dict[str, str] | None:
    """Extract the absolute device-local recording clock from DJI SRT cues."""
    stamps: list[str] = []
    for match in _RECORDING_DATETIME.finditer(text):
        year, month, day, hour, minute, second, milliseconds = match.groups()
        stamps.append(f"{year}-{month}-{day} {hour}:{minute}:{second}.{milliseconds}")
    if not stamps:
        return None
    return {"started_at_local": stamps[0], "ended_at_local": stamps[-1]}


_METRES_PER_DEGREE_LAT = 111_320.0
_ROUTE_SAMPLE_MIN_GAP_M = 15.0
_MATCH_SAMPLE_LIMIT = 600


def _route_line_coordinates(task_record: dict[str, Any] | None) -> list[tuple[float, float]]:
    if not isinstance(task_record, dict):
        return []
    route = task_record.get("route")
    geometry = route.get("geometry") if isinstance(route, dict) else None
    coordinates = geometry.get("coordinates") if isinstance(geometry, dict) else None
    if not isinstance(coordinates, list):
        return []
    cleaned: list[tuple[float, float]] = []
    for item in coordinates:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            longitude, latitude = float(item[0]), float(item[1])
        except (TypeError, ValueError):
            continue
        if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
            continue
        if not cleaned or (longitude, latitude) != cleaned[-1]:
            cleaned.append((longitude, latitude))
    return cleaned


def _point_segment_distance_m(point: tuple[float, float], start: tuple[float, float],
                              end: tuple[float, float]) -> float:
    """Equirectangular point-to-segment distance in metres for a local patch."""
    latitude = (point[1] + start[1] + end[1]) / 3
    scale_x = max(cos(radians(latitude)) * _METRES_PER_DEGREE_LAT, 1.0)
    point_x, point_y = point[0] * scale_x, point[1] * _METRES_PER_DEGREE_LAT
    start_x, start_y = start[0] * scale_x, start[1] * _METRES_PER_DEGREE_LAT
    end_x, end_y = end[0] * scale_x, end[1] * _METRES_PER_DEGREE_LAT
    delta_x, delta_y = end_x - start_x, end_y - start_y
    if delta_x == 0 and delta_y == 0:
        return ((point_x - start_x) ** 2 + (point_y - start_y) ** 2) ** 0.5
    projection = ((point_x - start_x) * delta_x + (point_y - start_y) * delta_y) / (delta_x ** 2 + delta_y ** 2)
    projection = max(0.0, min(1.0, projection))
    return ((point_x - start_x - projection * delta_x) ** 2
            + (point_y - start_y - projection * delta_y) ** 2) ** 0.5


def _point_polyline_distance_m(point: tuple[float, float], polyline: list[tuple[float, float]]) -> float:
    return min(_point_segment_distance_m(point, start, end) for start, end in zip(polyline, polyline[1:]))


def _thin_polyline(coordinates: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Drop vertices closer than the sampling gap; statistics keep sub-15 m accuracy."""
    if len(coordinates) <= 2:
        return coordinates
    kept = [coordinates[0]]
    for point in coordinates[1:-1]:
        if _point_segment_distance_m(point, kept[-1], point) >= _ROUTE_SAMPLE_MIN_GAP_M:
            kept.append(point)
    kept.append(coordinates[-1])
    return kept


def analyse_route_match(points: list[dict[str, Any]],
                        task_record: dict[str, Any] | None) -> dict[str, Any] | None:
    """Compare the SRT ground track against the task route snapshot."""
    if not isinstance(task_record, dict):
        return None
    route = _route_line_coordinates(task_record)
    if len(route) < 2:
        return {"status": "no_route", "message": "任务记录中没有可用的路线坐标，无法比对素材归属。"}
    if len(points) < 5:
        return {"status": "no_gps", "message": "SRT 定位点过少，无法与任务路线比对。"}
    polyline = _thin_polyline(route)
    sample = points if len(points) <= _MATCH_SAMPLE_LIMIT else [
        points[index] for index in range(0, len(points), ceil(len(points) / _MATCH_SAMPLE_LIMIT))
    ]
    deviations = sorted(
        _point_polyline_distance_m((point["longitude"], point["latitude"]), polyline)
        for point in sample
    )
    count = len(deviations)
    median = deviations[count // 2]
    p90 = deviations[min(count - 1, int(count * 0.9))]
    within_30m = sum(1 for value in deviations if value <= 30) / count * 100
    result: dict[str, Any] = {
        "median_deviation_m": round(median, 1),
        "p90_deviation_m": round(p90, 1),
        "max_deviation_m": round(deviations[-1], 1),
        "within_30m_percent": round(within_30m, 1),
        "sample_points": count,
    }
    # Take-off and landing hover beyond the route ends are normal, so the
    # verdict follows the median and the on-line share, not peak deviation.
    if median <= 25 and within_30m >= 60:
        result.update(status="matched",
                      message=f"轨迹与任务路线吻合（中位偏差 {median:.0f} 米，{within_30m:.0f}% 定位点在路线 30 米内），素材应属于本任务。")
    elif median <= 80:
        result.update(status="near",
                      message=f"轨迹接近任务路线（中位偏差 {median:.0f} 米），请人工确认素材归属。")
    else:
        result.update(status="off_route",
                      message=f"轨迹明显偏离任务路线（中位偏差 {median:.0f} 米），该素材可能不属于本任务。")
    return result


def interpolate_telemetry(points: list[dict[str, Any]], time_seconds: float) -> dict[str, Any] | None:
    if not points or not (time_seconds >= 0):
        return None
    if time_seconds <= points[0]["time_seconds"]:
        return dict(points[0]) if points[0]["time_seconds"] - time_seconds <= 2 else None
    if time_seconds >= points[-1]["time_seconds"]:
        return dict(points[-1]) if time_seconds - points[-1]["time_seconds"] <= 2 else None

    low, high = 0, len(points) - 1
    while low + 1 < high:
        middle = (low + high) // 2
        if points[middle]["time_seconds"] <= time_seconds:
            low = middle
        else:
            high = middle
    before, after = points[low], points[high]
    span = after["time_seconds"] - before["time_seconds"]
    if span <= 0:
        return dict(before)
    fraction = (time_seconds - before["time_seconds"]) / span
    result: dict[str, Any] = {"time_seconds": time_seconds}
    for name in ("latitude", "longitude", "altitude_m"):
        first, second = before.get(name), after.get(name)
        result[name] = (first + (second - first) * fraction
                        if first is not None and second is not None
                        else first if first is not None else second)
    for name in ("heading_deg", "gimbal_pitch_deg"):
        first, second = before.get(name), after.get(name)
        if first is None or second is None:
            result[name] = first if first is not None else second
        elif name == "heading_deg":
            delta = (second - first + 180) % 360 - 180
            result[name] = (first + delta * fraction) % 360
        else:
            result[name] = first + (second - first) * fraction
    result["interpolation_gap_seconds"] = span
    return result


class VideoSurveyStore:
    def __init__(self, storage_dir: Path = DEFAULT_STORAGE) -> None:
        self.storage_dir = storage_dir
        self.task_storage_dir = BACKEND_ROOT / "user_data" / "tasks"

    def _directory(self, job_id: str) -> Path:
        if not _JOB_ID.fullmatch(job_id):
            raise VideoSurveyError("视频任务编号无效。", http_status=404)
        directory = self.storage_dir / job_id
        if directory.is_dir():
            return directory
        for candidate in self.task_storage_dir.glob(f"*/media/video-surveys/{job_id}"):
            if candidate.is_dir():
                return candidate
        raise VideoSurveyError("找不到该视频任务。", http_status=404)

    async def create(self, video: UploadFile, srt: UploadFile, task_id: str | None = None,
                     task_record: dict[str, Any] | None = None) -> dict[str, Any]:
        video_name = Path(video.filename or "").name
        srt_name = Path(srt.filename or "").name
        if Path(video_name).suffix.lower() not in ALLOWED_VIDEO_SUFFIXES:
            raise VideoSurveyError("请选择 MP4、MOV 或 M4V 视频文件。")
        if Path(srt_name).suffix.lower() != ".srt":
            raise VideoSurveyError("请同时选择与视频对应的 .srt 文件。")
        srt_bytes = await srt.read(MAX_SRT_BYTES + 1)
        if not srt_bytes:
            raise VideoSurveyError("SRT 文件为空。")
        if len(srt_bytes) > MAX_SRT_BYTES:
            raise VideoSurveyError("SRT 文件不能超过 32 MiB。")
        try:
            srt_text = srt_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                srt_text = srt_bytes.decode("gb18030")
            except UnicodeDecodeError as error:
                # 两个解码都失败多半说明收到的字节被截断在多字节字符中间，
                # 而不是文件本身编码不对；报出字节数帮助定位。
                raise VideoSurveyError(
                    f"无法解码 SRT（收到 {len(srt_bytes)} 字节）；若原文件是正常字幕，"
                    "多为上传中断导致的传输截断，请重试。") from error

        job_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
        directory = (self.task_storage_dir / task_id / "media" / "video-surveys" / job_id
                     if task_id else self.storage_dir / job_id)
        directory.mkdir(parents=True, exist_ok=False)
        video_path = directory / ("video" + Path(video_name).suffix.lower())
        total_bytes = 0
        try:
            with video_path.open("wb") as output:
                while True:
                    chunk = await video.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    if total_bytes > MAX_VIDEO_BYTES:
                        raise VideoSurveyError("视频文件不能超过 8 GiB。")
                    output.write(chunk)
            if total_bytes == 0:
                raise VideoSurveyError("视频文件为空。")
            (directory / "telemetry.srt").write_bytes(srt_bytes)
            points = parse_srt_telemetry(srt_text)
            recording = parse_srt_recording_window(srt_text)
            task_match = analyse_route_match(points, task_record) if task_id else None
            metadata = {
                "job_id": job_id,
                "task_id": task_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "video_name": video_name,
                "srt_name": srt_name,
                "video_file": video_path.name,
                "video_bytes": total_bytes,
                "telemetry_points": len(points),
                "has_gps": bool(points),
                "recording": recording,
                "task_match": task_match,
                "message": "已读取 SRT 时间与 GPS，可在视频播放时查看对应地图位置。" if points else
                    "视频已导入，但 SRT 中没有识别到经纬度；请确认该视频生成了带 GPS 的视频字幕 SRT。",
            }
            (directory / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            (directory / "telemetry.json").write_text(json.dumps(points, ensure_ascii=False) + "\n", encoding="utf-8")
            (directory / "targets.json").write_text("[]\n", encoding="utf-8")
            return self.job(job_id)
        except Exception:
            try:
                video_path.unlink(missing_ok=True)
                (directory / "telemetry.srt").unlink(missing_ok=True)
                directory.rmdir()
            except OSError:
                pass
            raise

    async def create_srt_only(self, srt: UploadFile, video_name: str, task_id: str | None = None,
                              task_record: dict[str, Any] | None = None) -> dict[str, Any]:
        """Import telemetry only; the video stays on the user's machine.

        The full clip would cost a multi-gigabyte transfer, and everything
        except server-side frame extraction (map linkage, capture, archive)
        works from the SRT plus the browser's own file handle.
        """
        srt_name = Path(srt.filename or "").name
        if Path(srt_name).suffix.lower() != ".srt":
            raise VideoSurveyError("请同时选择与视频对应的 .srt 文件。")
        srt_bytes = await srt.read(MAX_SRT_BYTES + 1)
        if not srt_bytes:
            raise VideoSurveyError("SRT 文件为空。")
        if len(srt_bytes) > MAX_SRT_BYTES:
            raise VideoSurveyError("SRT 文件不能超过 32 MiB。")
        try:
            srt_text = srt_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                srt_text = srt_bytes.decode("gb18030")
            except UnicodeDecodeError as error:
                # 两个解码都失败多半说明收到的字节被截断在多字节字符中间，
                # 而不是文件本身编码不对；报出字节数帮助定位。
                raise VideoSurveyError(
                    f"无法解码 SRT（收到 {len(srt_bytes)} 字节）；若原文件是正常字幕，"
                    "多为上传中断导致的传输截断，请重试。") from error

        job_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
        directory = (self.task_storage_dir / task_id / "media" / "video-surveys" / job_id
                     if task_id else self.storage_dir / job_id)
        directory.mkdir(parents=True, exist_ok=False)
        try:
            (directory / "telemetry.srt").write_bytes(srt_bytes)
            points = parse_srt_telemetry(srt_text)
            recording = parse_srt_recording_window(srt_text)
            task_match = analyse_route_match(points, task_record) if task_id else None
            metadata = {
                "job_id": job_id,
                "task_id": task_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "video_name": video_name,
                "srt_name": srt_name,
                "video_file": None,
                "video_bytes": 0,
                "has_video": False,
                "telemetry_points": len(points),
                "has_gps": bool(points),
                "recording": recording,
                "task_match": task_match,
                "message": "已读取 SRT 轨迹；视频保留在本机，预览与截帧直接使用本地文件，"
                           "需要服务器端逐帧分析时可再上传完整视频。" if points else
                           "SRT 已导入，但没有识别到经纬度；请确认该视频生成了带 GPS 的视频字幕 SRT。",
            }
            (directory / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            (directory / "telemetry.json").write_text(json.dumps(points, ensure_ascii=False) + "\n", encoding="utf-8")
            (directory / "targets.json").write_text("[]\n", encoding="utf-8")
            return self.job(job_id)
        except Exception:
            for path in directory.iterdir():
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                directory.rmdir()
            except OSError:
                pass
            raise

    async def attach_video(self, job_id: str, video: UploadFile) -> dict[str, Any]:
        """Attach the full clip later, e.g. before server-side frame extraction.

        Idempotent: a retry after an upload whose response was lost returns
        the finished job instead of erroring.
        """
        directory = self._directory(job_id)
        metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
        if metadata.get("has_video"):
            return self.job(job_id)
        video_name = Path(video.filename or "").name
        if Path(video_name).suffix.lower() not in ALLOWED_VIDEO_SUFFIXES:
            raise VideoSurveyError("请选择 MP4、MOV 或 M4V 视频文件。")
        # 先写临时名再原子替换：响应丢失后的重复上传不会留下半截视频。
        video_path = directory / ("video" + Path(video_name).suffix.lower())
        pending_path = directory / (uuid.uuid4().hex[:8] + ".uploading")
        total_bytes = 0
        try:
            with pending_path.open("wb") as output:
                while True:
                    chunk = await video.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    if total_bytes > MAX_VIDEO_BYTES:
                        raise VideoSurveyError("视频文件不能超过 8 GiB。")
                    output.write(chunk)
            if total_bytes == 0:
                raise VideoSurveyError("视频文件为空。")
            pending_path.replace(video_path)
        except Exception:
            pending_path.unlink(missing_ok=True)
            raise
        metadata.update(video_name=video_name or metadata.get("video_name"),
                        video_file=video_path.name, video_bytes=total_bytes, has_video=True)
        (directory / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return self.job(job_id)

    def job(self, job_id: str) -> dict[str, Any]:
        directory = self._directory(job_id)
        metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
        telemetry = json.loads((directory / "telemetry.json").read_text(encoding="utf-8"))
        targets_path = directory / "targets.json"
        targets = json.loads(targets_path.read_text(encoding="utf-8")) if targets_path.is_file() else []
        has_video = bool(metadata.get("video_file"))
        return {**metadata, "has_video": has_video, "telemetry": telemetry, "targets": targets,
                "video_url": f"/api/video-surveys/{job_id}/video" if has_video else None}

    def video_path(self, job_id: str) -> Path:
        directory = self._directory(job_id)
        metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
        video_file = metadata.get("video_file")
        if not video_file:
            raise VideoSurveyError("该视频任务没有上传完整视频；自适应筛帧前需要先上传。", http_status=409)
        path = directory / video_file
        if not path.is_file():
            raise VideoSurveyError("视频文件不存在。", http_status=404)
        return path

    def add_target(self, job_id: str, time_seconds: float, label: str = "") -> dict[str, Any]:
        directory = self._directory(job_id)
        points = json.loads((directory / "telemetry.json").read_text(encoding="utf-8"))
        position = interpolate_telemetry(points, time_seconds)
        if position is None:
            raise VideoSurveyError("当前视频时刻没有可用的 GPS 位置。")
        targets_path = directory / "targets.json"
        targets = json.loads(targets_path.read_text(encoding="utf-8")) if targets_path.is_file() else []
        target = {
            "id": uuid.uuid4().hex[:12],
            "label": label.strip()[:60] or f"近拍点 {len(targets) + 1}",
            **position,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        targets.append(target)
        temporary = targets_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(targets, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(targets_path)
        return target

    def remove_target(self, job_id: str, target_id: str) -> list[dict[str, Any]]:
        directory = self._directory(job_id)
        targets_path = directory / "targets.json"
        targets = json.loads(targets_path.read_text(encoding="utf-8")) if targets_path.is_file() else []
        updated = [target for target in targets if target.get("id") != target_id]
        if len(updated) == len(targets):
            raise VideoSurveyError("找不到该近拍点。", http_status=404)
        temporary = targets_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(targets_path)
        return updated


video_surveys = VideoSurveyStore()

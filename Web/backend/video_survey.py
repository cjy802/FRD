"""Local DJI video + SRT review sessions for locating follow-up photos.

The video is stored for browser playback only. This module parses the SRT
telemetry and never runs image inference on video frames.
"""

from __future__ import annotations

import json
import re
import os
import shutil
import uuid
from datetime import datetime, timezone
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

    @staticmethod
    def _allowed_roots() -> list[dict[str, str]]:
        candidates: list[tuple[str, Path]] = []
        home = Path.home()
        candidates.extend((label, home / folder) for label, folder in (
            ("桌面", "Desktop"), ("下载", "Downloads"), ("视频", "Videos"), ("文档", "Documents"),
        ))
        if os.name == "nt":
            try:
                import ctypes

                drive_mask = ctypes.windll.kernel32.GetLogicalDrives()
                candidates.extend((f"{chr(65 + index)} 盘", Path(f"{chr(65 + index)}:/"))
                                  for index in range(26) if drive_mask & (1 << index))
            except (AttributeError, OSError):
                candidates.append(("D 盘", Path("D:/")))
        else:
            candidates.append(("项目文件夹", BACKEND_ROOT.parents[1]))

        roots: list[dict[str, str]] = []
        seen: set[str] = set()
        for label, candidate in candidates:
            try:
                if not candidate.is_dir():
                    continue
                resolved = candidate.resolve()
            except OSError:
                continue
            key = os.path.normcase(str(resolved))
            if key in seen:
                continue
            seen.add(key)
            roots.append({"label": label, "path": str(resolved)})
        return roots

    def _root_for(self, path: Path) -> dict[str, str] | None:
        for root in sorted(self._allowed_roots(), key=lambda item: len(item["path"]), reverse=True):
            try:
                path.relative_to(Path(root["path"]))
                return root
            except ValueError:
                continue
        return None

    def browse(self, path: str | None, kind: str) -> dict[str, Any]:
        if kind not in {"video", "srt"}:
            raise VideoSurveyError("文件类型无效。")
        roots = self._allowed_roots()
        if not roots:
            raise VideoSurveyError("找不到可浏览的本机文件夹。", http_status=503)
        if path:
            try:
                current = Path(path).resolve(strict=True)
            except (OSError, RuntimeError) as error:
                raise VideoSurveyError("该文件夹不存在或无法访问。") from error
        else:
            desktop = next((root for root in roots if root["label"] == "桌面"), roots[0])
            current = Path(desktop["path"])
        root = self._root_for(current)
        if root is None or not current.is_dir():
            raise VideoSurveyError("只能浏览桌面、下载、视频、文档或本机磁盘目录。", http_status=403)

        suffixes = ALLOWED_VIDEO_SUFFIXES if kind == "video" else {".srt"}
        directories: list[dict[str, Any]] = []
        files: list[dict[str, Any]] = []
        try:
            for child in current.iterdir():
                if child.name.startswith("."):
                    continue
                try:
                    resolved = child.resolve(strict=True)
                    resolved.relative_to(Path(root["path"]))
                    if resolved.is_dir():
                        directories.append({"name": child.name, "path": str(resolved), "is_directory": True})
                    elif resolved.is_file() and resolved.suffix.lower() in suffixes:
                        files.append({"name": child.name, "path": str(resolved), "is_directory": False,
                                      "size_bytes": resolved.stat().st_size})
                except (OSError, RuntimeError, ValueError):
                    continue
        except OSError as error:
            raise VideoSurveyError(f"无法读取该文件夹：{error}", http_status=403) from error
        directories.sort(key=lambda item: item["name"].casefold())
        files.sort(key=lambda item: item["name"].casefold())
        entries = directories + files
        truncated = len(entries) > 2000
        entries = entries[:2000]
        parent = current.parent if current != Path(root["path"]) else None
        return {"kind": kind, "roots": roots, "root": root, "current_path": str(current),
                "parent_path": str(parent) if parent is not None else None,
                "entries": entries, "truncated": truncated}

    def _selected_file(self, raw_path: str, suffixes: set[str], description: str) -> Path:
        try:
            path = Path(raw_path).resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise VideoSurveyError(f"找不到所选{description}文件。") from error
        if self._root_for(path) is None or not path.is_file():
            raise VideoSurveyError(f"所选{description}必须位于可浏览的本机文件夹内。", http_status=403)
        if path.suffix.lower() not in suffixes:
            raise VideoSurveyError(f"所选文件不是支持的{description}格式。")
        return path

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

    async def create(self, video: UploadFile, srt: UploadFile) -> dict[str, Any]:
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
                raise VideoSurveyError("无法读取 SRT 编码，请另存为 UTF-8 后重试。") from error

        job_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
        directory = self.storage_dir / job_id
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
            metadata = {
                "job_id": job_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "video_name": video_name,
                "srt_name": srt_name,
                "video_file": video_path.name,
                "video_bytes": total_bytes,
                "telemetry_points": len(points),
                "has_gps": bool(points),
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

    def create_from_paths(self, video_path: str, srt_path: str, task_id: str | None = None) -> dict[str, Any]:
        video_source = self._selected_file(video_path, ALLOWED_VIDEO_SUFFIXES, "视频")
        srt_source = self._selected_file(srt_path, {".srt"}, "SRT")
        video_size = video_source.stat().st_size
        if not video_size:
            raise VideoSurveyError("视频文件为空。")
        if video_size > MAX_VIDEO_BYTES:
            raise VideoSurveyError("视频文件不能超过 8 GiB。")
        srt_size = srt_source.stat().st_size
        if not srt_size:
            raise VideoSurveyError("SRT 文件为空。")
        if srt_size > MAX_SRT_BYTES:
            raise VideoSurveyError("SRT 文件不能超过 32 MiB。")
        try:
            srt_bytes = srt_source.read_bytes()
            srt_text = srt_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                srt_text = srt_bytes.decode("gb18030")
            except UnicodeDecodeError as error:
                raise VideoSurveyError("无法读取 SRT 编码，请另存为 UTF-8 后重试。") from error
        except OSError as error:
            raise VideoSurveyError(f"无法读取 SRT 文件：{error}") from error

        job_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
        directory = (self.task_storage_dir / task_id / "media" / "video-surveys" / job_id
                     if task_id else self.storage_dir / job_id)
        directory.mkdir(parents=True, exist_ok=False)
        video_path_out = directory / ("video" + video_source.suffix.lower())
        try:
            with video_source.open("rb") as source, video_path_out.open("wb") as output:
                shutil.copyfileobj(source, output, length=CHUNK_BYTES)
            (directory / "telemetry.srt").write_bytes(srt_bytes)
            points = parse_srt_telemetry(srt_text)
            metadata = {
                "job_id": job_id,
                "task_id": task_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "video_name": video_source.name,
                "srt_name": srt_source.name,
                "video_file": video_path_out.name,
                "video_bytes": video_size,
                "telemetry_points": len(points),
                "has_gps": bool(points),
                "message": "已读取 SRT 时间与 GPS，可在视频播放时查看对应地图位置。" if points else
                    "视频已导入，但 SRT 中没有识别到经纬度；请确认该视频生成了带 GPS 的视频字幕 SRT。",
            }
            (directory / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            (directory / "telemetry.json").write_text(json.dumps(points, ensure_ascii=False) + "\n", encoding="utf-8")
            (directory / "targets.json").write_text("[]\n", encoding="utf-8")
            return self.job(job_id)
        except OSError as error:
            for path in directory.iterdir():
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                directory.rmdir()
            except OSError:
                pass
            raise VideoSurveyError(f"无法保存所选视频或 SRT：{error}") from error

    def job(self, job_id: str) -> dict[str, Any]:
        directory = self._directory(job_id)
        metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
        telemetry = json.loads((directory / "telemetry.json").read_text(encoding="utf-8"))
        targets_path = directory / "targets.json"
        targets = json.loads(targets_path.read_text(encoding="utf-8")) if targets_path.is_file() else []
        return {**metadata, "telemetry": telemetry, "targets": targets,
                "video_url": f"/api/video-surveys/{job_id}/video"}

    def video_path(self, job_id: str) -> Path:
        directory = self._directory(job_id)
        metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
        path = directory / metadata["video_file"]
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

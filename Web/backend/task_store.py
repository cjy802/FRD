"""Persistent mission records that bind a route, weather review and survey media."""

from __future__ import annotations

import json
import re
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parent
DEFAULT_STORAGE = BACKEND_ROOT / "user_data" / "tasks"
_TASK_ID = re.compile(r"\d{14}-[0-9a-f]{8}")
MAX_TASK_BYTES = 16 * 1024 * 1024
MAX_FRAME_BYTES = 40 * 1024 * 1024
MAX_FRAMES_PER_TASK = 300


def _route_preview_polyline(coordinates: Any, limit: int = 64) -> list[list[float]]:
    """任务列表缩略图用的抽稀路线（WGS84 [经度, 纬度]）。

    保首尾、按索引等距取样；原始折线动辄数千点，全量进列表响应没有意义。
    """
    valid: list[list[float]] = []
    for point in coordinates if isinstance(coordinates, list) else []:
        if (isinstance(point, (list, tuple)) and len(point) >= 2
                and isinstance(point[0], (int, float)) and isinstance(point[1], (int, float))):
            valid.append([float(point[0]), float(point[1])])
    if len(valid) < 2:
        return []
    if len(valid) <= limit:
        return valid
    picked = [valid[0]]
    step = (len(valid) - 1) / (limit - 1)
    picked.extend(valid[round(index * step)] for index in range(1, limit - 1))
    picked.append(valid[-1])
    return picked


class TaskError(Exception):
    def __init__(self, message: str, http_status: int = 400) -> None:
        super().__init__(message)
        self.http_status = http_status


class TaskStore:
    def __init__(self, storage_dir: Path = DEFAULT_STORAGE) -> None:
        self.storage_dir = storage_dir

    def _directory(self, task_id: str) -> Path:
        if not _TASK_ID.fullmatch(task_id):
            raise TaskError("任务编号无效。", http_status=404)
        path = self.storage_dir / task_id
        if not path.is_dir() or not (path / "task.json").is_file():
            raise TaskError("找不到该任务。", http_status=404)
        return path

    def create(self, route: dict[str, Any], departure_time: str, departure_auto_managed: bool = True,
               name: str | None = None, weather: dict[str, Any] | None = None,
               aircraft: dict[str, Any] | None = None) -> dict[str, Any]:
        task_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
        directory = self.storage_dir / task_id
        directory.mkdir(parents=True, exist_ok=False)
        record: dict[str, Any] = {
            "task_id": task_id,
            "name": name.strip() if isinstance(name, str) and name.strip() else None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "status": "planning",
            "route": route,
            "departure_time": departure_time,
            "departure_auto_managed": departure_auto_managed,
            "weather": weather,
            "aircraft": aircraft,
            "export_file_name": None,
            "flight_started_at": None,
            "video_survey_id": None,
            "analysis_job_ids": [],
            "analysis_targets": [],
        }
        self._write(directory, record)
        (directory / "frames").mkdir()
        return record

    def get(self, task_id: str) -> dict[str, Any]:
        return json.loads((self._directory(task_id) / "task.json").read_text(encoding="utf-8"))

    def list_summaries(self) -> list[dict[str, Any]]:
        if not self.storage_dir.is_dir():
            return []
        summaries = []
        for directory in self.storage_dir.iterdir():
            if not directory.is_dir() or not _TASK_ID.fullmatch(directory.name):
                continue
            try:
                record = json.loads((directory / "task.json").read_text(encoding="utf-8"))
                route = record.get("route") or {}
                anchors = route.get("anchors") or []
                road_names = list(dict.fromkeys(name for name in route.get("road_names", []) if isinstance(name, str) and name.strip()))
                if road_names:
                    route_summary = " → ".join(road_names[:3])
                    if len(road_names) > 3:
                        route_summary += " …"
                elif len(anchors) >= 2:
                    start, end = anchors[0], anchors[-1]
                    route_summary = (f"{float(start['latitude']):.4f}, {float(start['longitude']):.4f} → "
                                     f"{float(end['latitude']):.4f}, {float(end['longitude']):.4f}")
                else:
                    route_summary = "未命名航线"
                weather = record.get("weather") or {}
                risk = weather.get("weather_risk") or {}
                weather_summary = risk.get("summary") or {}
                frames_path = directory / "frames" / "frames.json"
                frames = json.loads(frames_path.read_text(encoding="utf-8")) if frames_path.is_file() else []
                summaries.append({
                    "task_id": record["task_id"],
                    "name": record.get("name") or route_summary,
                    "created_at": record.get("created_at"),
                    "updated_at": record.get("updated_at"),
                    "status": record.get("status", "planning"),
                    "route_summary": route_summary,
                    "route_preview": _route_preview_polyline((route.get("geometry") or {}).get("coordinates")),
                    "distance_km": route.get("distance_km"),
                    "departure_time": record.get("departure_time"),
                    "weather_level": risk.get("level"),
                    "max_wind_m_s": weather_summary.get("max_wind_m_s"),
                    "max_rain_6h_mm": weather_summary.get("max_rain_6h_mm"),
                    "drone_model": (record.get("aircraft") or {}).get("drone_model"),
                    "video_survey_id": record.get("video_survey_id"),
                    "frame_count": len(frames),
                })
            except (OSError, ValueError, KeyError, TypeError):
                continue
        summaries.sort(key=lambda item: (item.get("updated_at") or item.get("created_at") or ""), reverse=True)
        return summaries

    def delete(self, task_id: str) -> None:
        directory = self._directory(task_id)
        root = self.storage_dir.resolve(strict=True)
        target = directory.resolve(strict=True)
        if directory.is_symlink() or target == root or target.parent != root:
            raise TaskError("任务目录位置无效，无法删除。", http_status=400)
        try:
            shutil.rmtree(target)
        except OSError as error:
            raise TaskError(f"删除任务失败：{error}", http_status=500) from error

    def update(self, task_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        directory = self._directory(task_id)
        record = json.loads((directory / "task.json").read_text(encoding="utf-8"))
        for key, value in changes.items():
            if key in {"weather", "aircraft", "export_file_name", "flight_started_at", "video_survey_id", "status", "analysis_targets", "departure_time", "departure_auto_managed"}:
                record[key] = value
            elif key == "analysis_job_id" and value:
                ids = record.setdefault("analysis_job_ids", [])
                if value not in ids:
                    ids.append(value)
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._write(directory, record)
        return record

    def frame_directory(self, task_id: str) -> Path:
        return self._directory(task_id) / "frames"

    def directory(self, task_id: str) -> Path:
        return self._directory(task_id)

    def add_frame(self, task_id: str, file_name: str, payload: bytes, metadata: dict[str, Any]) -> dict[str, Any]:
        directory = self.frame_directory(task_id)
        index_path = directory / "frames.json"
        frames = json.loads(index_path.read_text(encoding="utf-8")) if index_path.is_file() else []
        if len(frames) >= MAX_FRAMES_PER_TASK:
            raise TaskError(f"每个任务最多保留 {MAX_FRAMES_PER_TASK} 张视频关键帧；可先移除不需要的帧。", http_status=409)
        if Path(file_name).suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            raise TaskError("任务关键帧只支持 JPG、PNG 或 WebP。")
        if not payload:
            raise TaskError("关键帧文件为空。")
        if len(payload) > MAX_FRAME_BYTES:
            raise TaskError("单张关键帧不能超过 40 MiB。", http_status=413)
        frame_id = uuid.uuid4().hex[:12]
        suffix = Path(file_name).suffix.lower()
        stored_name = f"{frame_id}{suffix}"
        (directory / stored_name).write_bytes(payload)
        frame = {"frame_id": frame_id, "name": Path(file_name).name, "file_name": stored_name,
                 "bytes": len(payload), "created_at": datetime.now(timezone.utc).isoformat(), **metadata}
        frames.append(frame)
        temporary = index_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(frames, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, index_path)
        return frame

    def remove_frame(self, task_id: str, frame_id: str) -> list[dict[str, Any]]:
        path = self.frame_path(task_id, frame_id)
        index_path = self.frame_directory(task_id) / "frames.json"
        frames = self.frames(task_id)
        updated = [item for item in frames if item.get("frame_id") != frame_id]
        path.unlink(missing_ok=True)
        temporary = index_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, index_path)
        return updated

    def remove_all_frames(self, task_id: str) -> None:
        directory = self.frame_directory(task_id)
        index_path = directory / "frames.json"
        for frame in self.frames(task_id):
            file_name = frame.get("file_name")
            if file_name:
                (directory / file_name).unlink(missing_ok=True)
        temporary = index_path.with_suffix(".tmp")
        temporary.write_text("[]\n", encoding="utf-8")
        os.replace(temporary, index_path)

    def frames(self, task_id: str) -> list[dict[str, Any]]:
        index_path = self.frame_directory(task_id) / "frames.json"
        return json.loads(index_path.read_text(encoding="utf-8")) if index_path.is_file() else []

    def frame_path(self, task_id: str, frame_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{12}", frame_id):
            raise TaskError("关键帧编号无效。", http_status=404)
        frame = next((item for item in self.frames(task_id) if item.get("frame_id") == frame_id), None)
        if frame is None:
            raise TaskError("找不到该任务关键帧。", http_status=404)
        path = self.frame_directory(task_id) / frame["file_name"]
        if not path.is_file():
            raise TaskError("任务关键帧文件不存在。", http_status=404)
        return path

    @staticmethod
    def _write(directory: Path, record: dict[str, Any]) -> None:
        payload = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
        if len(payload.encode("utf-8")) > MAX_TASK_BYTES:
            raise TaskError("任务记录过大，请减少航线点数后重试。", http_status=413)
        target = directory / "task.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(target)


tasks = TaskStore()

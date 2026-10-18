"""Run low-cost visual overlap selection over task video in the CV environment."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parent
DEFAULT_PYTHON = Path("D:/anaconda/envs/yolov8-env/python.exe")
RUNNER = BACKEND_ROOT / "scripts" / "run_adaptive_frames.py"
TIMEOUT_SECONDS = 900


class AdaptiveVideoError(Exception):
    def __init__(self, message: str, http_status: int = 422) -> None:
        super().__init__(message)
        self.http_status = http_status


def _python_path() -> Path:
    value = os.environ.get("WATER_ANALYSIS_YOLO_PYTHON", "").strip()
    return Path(value) if value else DEFAULT_PYTHON


def select_frames(video_path: Path, telemetry: list[dict[str, Any]], output_dir: Path, *,
                  max_frames: int, scan_fps: float, straight_overlap_percent: float,
                  turn_overlap_percent: float, turn_angle_degrees: float,
                  camera_change_degrees: float) -> dict[str, Any]:
    python = _python_path()
    if not python.is_file():
        raise AdaptiveVideoError("找不到视频视觉分析 Python 环境。", http_status=503)
    if not RUNNER.is_file() or not video_path.is_file():
        raise AdaptiveVideoError("自适应切帧程序或任务视频不存在。", http_status=404)
    output_dir.mkdir(parents=True, exist_ok=True)
    telemetry_path = output_dir / "telemetry.json"
    telemetry_path.write_text(json.dumps(telemetry, ensure_ascii=False), encoding="utf-8")
    command = [
        str(python), "-B", str(RUNNER),
        "--video", str(video_path),
        "--telemetry", str(telemetry_path),
        "--output-dir", str(output_dir),
        "--max-frames", str(max_frames),
        "--scan-fps", str(scan_fps),
        "--straight-overlap", str(straight_overlap_percent),
        "--turn-overlap", str(turn_overlap_percent),
        "--turn-angle", str(turn_angle_degrees),
        "--camera-change", str(camera_change_degrees),
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=TIMEOUT_SECONDS, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as error:
        raise AdaptiveVideoError("视频画面扫描超时，请降低扫描频率或缩短视频。", http_status=504) from error
    finally:
        telemetry_path.unlink(missing_ok=True)
    result = None
    for line in reversed(completed.stdout.splitlines()):
        try:
            candidate = json.loads(line.strip())
        except ValueError:
            continue
        if isinstance(candidate, dict) and ("selected_frames" in candidate or "error" in candidate):
            result = candidate
            break
    if completed.returncode != 0 or result is None or result.get("error"):
        detail = (result or {}).get("error") or completed.stderr.strip()[-500:] or "自适应切帧进程失败。"
        raise AdaptiveVideoError(str(detail)[:600], http_status=422)
    manifest = output_dir / "selection_manifest.json"
    manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result

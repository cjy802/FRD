"""Post-flight photo analysis jobs: uploaded drone photos -> water segmentation.

The web backend never imports the ML stack. Uploaded photos are stored under
user_data/photo_analysis/<job id>/inputs and processed in a separate Python
environment, mirroring the FuXi subprocess pattern in live_weather.py. Users can
choose the semantic ONNX model or the YOLOv8-seg champion checkpoint. Results
(overlay PNGs, suspected masks, confidence maps, results.json) stay in the job
directory and are served back through the API. Suspected pixels are leads for
manual review, never depth/water-level conclusions.
"""

from __future__ import annotations

import json
import math
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parent
DEFAULT_STORAGE = BACKEND_ROOT / "user_data" / "photo_analysis"
DEFAULT_PYTHON = Path("D:/anaconda/envs/waterseg/python.exe")
DEFAULT_MODEL_DIR = BACKEND_ROOT.parents[1] / "model" / "artifacts" / "onnx"
DEFAULT_YOLO_PYTHON = Path("D:/anaconda/envs/yolov8-env/python.exe")
DEFAULT_YOLO_WEIGHTS = BACKEND_ROOT.parents[1] / "model" / "output" / "4090_champions" / "yoloj_mix4_best.pt"
RUNNER = BACKEND_ROOT / "scripts" / "run_water_onnx.py"
YOLO_RUNNER = BACKEND_ROOT / "scripts" / "run_water_yolo.py"
REMOTE_RUNNER = BACKEND_ROOT / "scripts" / "run_water_glm.py"
ONNX_MODEL_ID = "abcd-local"
YOLO_MODEL_ID = "yolo-champion"
REMOTE_MODEL_ID = "remote-glm-puddle"
MAX_PHOTOS_PER_JOB = 200
MAX_PHOTO_BYTES = 40 * 1024 * 1024
ALLOWED_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
JOB_TIMEOUT_SECONDS = 900
DEFAULT_CAMERA_FOV_DEGREES = 75.0
NADIR_PITCH_DEGREES = -60.0


class PhotoAnalysisError(Exception):
    def __init__(self, message: str, http_status: int = 400) -> None:
        super().__init__(message)
        self.http_status = http_status


def _configured_path(env_name: str, default: Path) -> Path:
    import os

    value = os.environ.get(env_name, "").strip()
    return Path(value) if value else default


def _configured_value(name: str, default: str = "") -> str:
    import os

    value = os.environ.get(name, "").strip()
    if value:
        return value
    try:
        from dotenv import dotenv_values

        return str(dotenv_values(BACKEND_ROOT / ".env").get(name) or default).strip()
    except (OSError, ImportError):
        return default


class PhotoAnalysisManager:
    def __init__(self, *, storage_dir: Path = DEFAULT_STORAGE, python_path: Path | None = None,
                 model_dir: Path | None = None, runner_path: Path | None = None,
                 yolo_python_path: Path | None = None, yolo_model_path: Path | None = None,
                 yolo_runner_path: Path | None = None) -> None:
        self.storage_dir = storage_dir
        self.python_path = python_path if python_path is not None else _configured_path("WATER_ANALYSIS_PYTHON", DEFAULT_PYTHON)
        self.model_dir = model_dir if model_dir is not None else _configured_path("WATER_ANALYSIS_MODEL_DIR", DEFAULT_MODEL_DIR)
        self.runner_path = runner_path if runner_path is not None else RUNNER
        self.yolo_python_path = (yolo_python_path if yolo_python_path is not None
                                 else _configured_path("WATER_ANALYSIS_YOLO_PYTHON", DEFAULT_YOLO_PYTHON))
        if yolo_model_path is not None:
            self.yolo_model_path = yolo_model_path
        elif runner_path is not None:
            # A custom ONNX runner is commonly injected by tests or callers; do
            # not silently route those isolated jobs into the machine's YOLO model.
            import os

            configured_yolo = os.environ.get("WATER_ANALYSIS_YOLO_MODEL", "").strip()
            self.yolo_model_path = Path(configured_yolo) if configured_yolo else None
        else:
            self.yolo_model_path = _configured_path("WATER_ANALYSIS_YOLO_MODEL", DEFAULT_YOLO_WEIGHTS)
        self.yolo_runner_path = yolo_runner_path if yolo_runner_path is not None else YOLO_RUNNER
        self.remote_runner_path = REMOTE_RUNNER
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._job_dirs: dict[str, Path] = {}

    def _model_options(self) -> list[dict[str, Any]]:
        registry_path = self.model_dir / "registry.json"
        registry_ok = registry_path.is_file()
        onnx_model_ok = False
        if registry_ok:
            try:
                registry = json.loads(registry_path.read_text(encoding="utf-8"))
                model_name = registry.get("default_model", "")
                onnx_model_ok = bool(model_name) and (self.model_dir / model_name).is_file()
            except (OSError, ValueError, TypeError):
                registry_ok = False
        onnx_ready = self.python_path.is_file() and registry_ok and onnx_model_ok and self.runner_path.is_file()
        onnx_message = "可用" if onnx_ready else "需要推理 Python、有效的 ONNX registry/权重和 runner"

        yolo_model_ok = self.yolo_model_path is not None and self.yolo_model_path.is_file()
        yolo_ready = (self.yolo_python_path.is_file() and yolo_model_ok and self.yolo_runner_path.is_file())
        yolo_message = "可用" if yolo_ready else "需要 yolov8-env、冠军 .pt 权重和 YOLO runner"
        remote_configured = bool(_configured_value("WATER_ANALYSIS_REMOTE_URL")
                                 and _configured_value("WATER_ANALYSIS_REMOTE_API_KEY")
                                 and _configured_value("WATER_ANALYSIS_REMOTE_MODEL", "glm-5.3-flash"))
        remote_ready = self.yolo_python_path.is_file() and self.remote_runner_path.is_file() and remote_configured
        remote_message = ("配置完整；首次分析会验证该模型端点的图像输入能力" if remote_ready else
                          "需要填写后端 .env 的远程 URL、API Key 和模型，并配置 yolov8-env")
        return [
            {"id": ONNX_MODEL_ID, "label": "ABCDWaveNet 本地混合模型", "description": "语义分割 · RGB 积水候选",
             "available": onnx_ready, "message": onnx_message},
            {"id": YOLO_MODEL_ID, "label": "YOLOv8n-seg 4090 混训冠军", "description": "实例分割 · Puddle/FRED/FloodNet/本地种子混训",
             "available": yolo_ready, "message": yolo_message},
            {"id": REMOTE_MODEL_ID, "label": "远程测试模型", "description": "远程视觉识别水坑候选；本机把返回的多边形栅格化为掩膜和叠图",
             "available": remote_ready, "message": remote_message},
        ]

    def health(self) -> dict[str, Any]:
        with self._lock:
            busy = any(job["state"] in ("pending", "running") for job in self._jobs.values())
        models = self._model_options()
        available = any(model["available"] for model in models)
        yolo_available = next((model["available"] for model in models if model["id"] == YOLO_MODEL_ID), False)
        default_model_id = YOLO_MODEL_ID if yolo_available else ONNX_MODEL_ID
        message = "照片分析组件已就绪。可选择本地分割模型或远程测试模型。" if available else (
            "照片分析不可用：" + "；".join(model["message"] for model in models)
        )
        return {"available": available, "busy": busy, "models": models,
                "default_model_id": default_model_id,
                "python": str(self.python_path), "model_dir": str(self.model_dir),
                "max_photos_per_job": MAX_PHOTOS_PER_JOB, "message": message}

    def submit(self, photos: list[tuple[str, bytes]], context: dict[str, Any] | None = None,
               model_id: str | None = None, task_id: str | None = None) -> str:
        if not photos:
            raise PhotoAnalysisError("没有收到照片文件。")
        if len(photos) > MAX_PHOTOS_PER_JOB:
            raise PhotoAnalysisError(f"一次最多 {MAX_PHOTOS_PER_JOB} 张照片。")
        health = self.health()
        if not health["available"]:
            raise PhotoAnalysisError(health["message"], http_status=503)
        selected_model_id = model_id or health["default_model_id"]
        selected_model = next((model for model in health["models"] if model["id"] == selected_model_id), None)
        if selected_model is None:
            raise PhotoAnalysisError("无法识别所选照片分析模型。")
        if not selected_model["available"]:
            raise PhotoAnalysisError(f"所选模型未就绪：{selected_model['message']}", http_status=503)
        with self._lock:
            if any(job["state"] in ("pending", "running") for job in self._jobs.values()):
                raise PhotoAnalysisError("已有分析任务在运行，请等待完成后再提交。", http_status=409)
            job_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
            job_dir = (BACKEND_ROOT / "user_data" / "tasks" / task_id / "media" / "photo-analysis" / job_id
                       if task_id else self.storage_dir / job_id)
            inputs = job_dir / "inputs"
            inputs.mkdir(parents=True, exist_ok=True)
            saved: list[dict[str, Any]] = []
            for index, (name, payload) in enumerate(photos):
                suffix = Path(name).suffix.lower()
                if suffix not in ALLOWED_SUFFIXES:
                    raise PhotoAnalysisError(f"不支持的文件类型：{name}")
                if not payload:
                    raise PhotoAnalysisError(f"空文件：{name}")
                if len(payload) > MAX_PHOTO_BYTES:
                    raise PhotoAnalysisError(f"文件过大（>{MAX_PHOTO_BYTES // (1024 * 1024)} MiB）：{name}")
                safe_name = f"{index:03d}-{Path(name).name}"
                (inputs / safe_name).write_bytes(payload)
                saved.append({"name": safe_name, "original_name": name, "bytes": len(payload)})
            job = {
                "job_id": job_id,
                "state": "pending",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "finished_at": None,
                "context": context or {},
                "task_id": task_id,
                "model_id": selected_model_id,
                "inputs": saved,
                "results": None,
                "error": None,
            }
            self._jobs[job_id] = job
            self._job_dirs[job_id] = job_dir
        thread = threading.Thread(target=self._process, args=(job_id,), daemon=True, name=f"photo-analysis-{job_id}")
        thread.start()
        return job_id

    def _enrich_with_ground_scale(self, payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        """Attach per-image ground-scale estimates using SRT altitude + camera FOV.

        Flat-ground, near-nadir assumption. Oblique frames (gimbal pitch above
        NADIR_PITCH_DEGREES) get a coarse 1/cos^2 correction and are flagged so the
        UI can downgrade their alert level. Frames without altitude stay null.
        Frame metadata is matched to images by submission order, mirroring the
        equal-length check in the submit endpoint.
        """
        frames = context.get("frames")
        frames = frames if isinstance(frames, list) else []
        try:
            fov_deg = float(context.get("camera_fov_deg") or DEFAULT_CAMERA_FOV_DEGREES)
        except (TypeError, ValueError):
            fov_deg = DEFAULT_CAMERA_FOV_DEGREES
        fov_deg = min(max(fov_deg, 30.0), 120.0)
        for index, image in enumerate(payload.get("images", [])):
            frame = frames[index] if index < len(frames) and isinstance(frames[index], dict) else {}
            altitude = frame.get("altitude_m")
            pitch = frame.get("gimbal_pitch_deg")
            width = int(image.get("width") or 0)
            height = int(image.get("height") or 0)
            image["altitude_m"] = float(altitude) if isinstance(altitude, (int, float)) and altitude > 0 else None
            image["gimbal_pitch_deg"] = float(pitch) if isinstance(pitch, (int, float)) else None
            image["camera_fov_deg"] = fov_deg
            image.update({"area_estimate": "none", "gsd_m": None, "ground_width_m": None,
                          "ground_height_m": None, "estimated_area_m2": None, "largest_patch_area_m2": None})
            if image["altitude_m"] is None or width <= 0 or height <= 0:
                continue
            ground_width = 2.0 * image["altitude_m"] * math.tan(math.radians(fov_deg) / 2.0)
            gsd = ground_width / width
            oblique = image["gimbal_pitch_deg"] is not None and image["gimbal_pitch_deg"] > NADIR_PITCH_DEGREES
            oblique_factor = 1.0
            if oblique:
                from_nadir = math.radians(90.0 - min(abs(image["gimbal_pitch_deg"]), 89.0))
                oblique_factor = 1.0 / max(math.cos(from_nadir) ** 2, 0.25)
            pixels = float(image.get("suspected_pixels") or 0)
            patch_areas = image.get("patch_area_pixels")
            largest_pixels = float(patch_areas[0]) if isinstance(patch_areas, list) and patch_areas else pixels
            image.update({
                "area_estimate": "oblique" if oblique else "nadir",
                "gsd_m": round(gsd, 4),
                "ground_width_m": round(ground_width, 2),
                "ground_height_m": round(gsd * height, 2),
                "estimated_area_m2": round(pixels * gsd * gsd * oblique_factor, 2),
                "largest_patch_area_m2": round(largest_pixels * gsd * gsd * oblique_factor, 2),
            })
        return payload

    def _process(self, job_id: str) -> None:
        job_dir = self._job_dirs[job_id]
        output_dir = job_dir / "outputs"
        job = self.job(job_id)
        environment = None
        if job["model_id"] == REMOTE_MODEL_ID:
            import os

            environment = os.environ.copy()
            environment.update({
                "WATER_ANALYSIS_REMOTE_URL": _configured_value("WATER_ANALYSIS_REMOTE_URL"),
                "WATER_ANALYSIS_REMOTE_API_KEY": _configured_value("WATER_ANALYSIS_REMOTE_API_KEY"),
                "WATER_ANALYSIS_REMOTE_MODEL": _configured_value("WATER_ANALYSIS_REMOTE_MODEL", "glm-5.3-flash"),
            })
            command = [str(self.yolo_python_path), "-B", str(self.remote_runner_path),
                       "--input-dir", str(job_dir / "inputs"), "--output-dir", str(output_dir)]
        elif job["model_id"] == YOLO_MODEL_ID:
            command = [
                str(self.yolo_python_path), "-B", str(self.yolo_runner_path),
                "--weights", str(self.yolo_model_path),
                "--input-dir", str(job_dir / "inputs"),
                "--output-dir", str(output_dir),
            ]
        else:
            command = [
                str(self.python_path), "-B", str(self.runner_path),
                "--model-dir", str(self.model_dir),
                "--input-dir", str(job_dir / "inputs"),
                "--output-dir", str(output_dir),
            ]
        try:
            with self._lock:
                self._jobs[job_id]["state"] = "running"
            completed = subprocess.run(
                command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=JOB_TIMEOUT_SECONDS, env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            payload = None
            for line in reversed(completed.stdout.splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        payload = json.loads(line)
                    except ValueError:
                        continue
                    if "error" in payload or "images" in payload:
                        break
            if completed.returncode != 0 or payload is None or "error" in payload:
                detail = (payload or {}).get("error") or completed.stderr.strip()[-500:] or "推理进程失败"
                secret = _configured_value("WATER_ANALYSIS_REMOTE_API_KEY")
                if secret:
                    detail = str(detail).replace(secret, "[已隐藏 API Key]")
                raise RuntimeError(detail)
            with self._lock:
                self._jobs[job_id]["results"] = self._enrich_with_ground_scale(payload, job["context"])
                self._jobs[job_id]["state"] = "done"
                self._jobs[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()
        except Exception as error:  # noqa: BLE001 - stored on the job for the UI
            with self._lock:
                self._jobs[job_id]["state"] = "error"
                detail = str(error)
                secret = _configured_value("WATER_ANALYSIS_REMOTE_API_KEY")
                if secret:
                    detail = detail.replace(secret, "[已隐藏 API Key]")
                self._jobs[job_id]["error"] = f"{type(error).__name__}: {detail}"
                self._jobs[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()

    def job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise PhotoAnalysisError("找不到该分析任务。", http_status=404)
            return json.loads(json.dumps(job))

    def output_file(self, job_id: str, name: str) -> Path:
        job = self.job(job_id)
        if job["state"] != "done":
            raise PhotoAnalysisError("任务尚未完成。", http_status=409)
        allowed: dict[str, str] = {}
        for image in job["results"]["images"]:
            for key in ("overlay", "suspected_mask", "confidence_map"):
                if image.get(key):
                    allowed[Path(image[key]).name] = f"outputs/{image[key]}"
            allowed[image["name"]] = f"inputs/{image['name']}"
        relative = allowed.get(name) or allowed.get(Path(name).name)
        if relative is None:
            raise PhotoAnalysisError("不允许的文件名。", http_status=404)
        base = self._job_dirs.get(job_id, self.storage_dir / job_id)
        target = (base / relative).resolve()
        if base.resolve() not in target.parents or not target.is_file():
            raise PhotoAnalysisError("找不到文件。", http_status=404)
        return target


photo_analysis = PhotoAnalysisManager()

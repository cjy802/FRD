"""Post-flight photo analysis jobs: uploaded drone photos -> water segmentation.

The web backend never imports the ML stack. Two job modes exist:

* ``legacy`` - the original whole-batch flow: uploaded photo bytes are stored
  under ``user_data/photo_analysis/<job id>/inputs`` (or the task media folder)
  and one subprocess processes the entire folder with fail-fast semantics.
  Kept for compatibility and the stub-runner tests.
* ``frames`` - the frame queue: jobs reference frame files that already live in
  ``user_data/tasks/<id>/frames`` by ``frame_id``. Frames can be appended while
  the job runs (streaming from the extraction phase), workers consume them in
  small batches through a manifest, runners append one ``results.jsonl`` line
  per frame so progress is visible per frame, single-frame failures are
  retried and never discard the rest of the queue. Progress is written through
  to ``job.json`` so a backend restart can resume a non-terminal job.

Results (overlay PNGs, suspected masks, confidence maps) stay in the job
directory and are served back through the API. Suspected pixels are leads for
manual review, never depth/water-level conclusions.
"""

from __future__ import annotations

import json
import math
import os
import queue
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

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

# --- frame-queue tuning -----------------------------------------------------
QUEUE_BATCH_SIZE = 8
QUEUE_MAX_ATTEMPTS = 3          # 首次 + 2 次自动重试
QUEUE_RETRY_DELAYS = (3.0, 10.0)
QUEUE_CIRCUIT_FAILURES = 8      # 连续失败且零成功 → 判定系统性故障，熔断中止
QUEUE_IDLE_AUTOCLOSE_SECONDS = 600
QUEUE_WORKERS = {REMOTE_MODEL_ID: 4, YOLO_MODEL_ID: 1, ONNX_MODEL_ID: 1}
QUEUE_BATCH_TIMEOUT_BASE = {REMOTE_MODEL_ID: 90, YOLO_MODEL_ID: 60, ONNX_MODEL_ID: 60}
QUEUE_BATCH_TIMEOUT_PER_IMAGE = {REMOTE_MODEL_ID: 150, YOLO_MODEL_ID: 20, ONNX_MODEL_ID: 30}
FALLBACK_INTERPRETATION = "识别结果为候选线索，需人工核对；像素占比不是面积，也不代表水深或水位结论。"

FrameResolver = Callable[[str, str], dict[str, Any]]
# resolver(task_id, frame_id) -> {"name": <unique file name>, "path": Path, "meta": {...}}


class PhotoAnalysisError(Exception):
    def __init__(self, message: str, http_status: int = 400) -> None:
        super().__init__(message)
        self.http_status = http_status


def _configured_path(env_name: str, default: Path) -> Path:
    value = os.environ.get(env_name, "").strip()
    return Path(value) if value else default


def _configured_value(name: str, default: str = "") -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    try:
        from dotenv import dotenv_values

        return str(dotenv_values(BACKEND_ROOT / ".env").get(name) or default).strip()
    except (OSError, ImportError):
        return default


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, text: str) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


class PhotoAnalysisManager:
    def __init__(self, *, storage_dir: Path = DEFAULT_STORAGE, python_path: Path | None = None,
                 model_dir: Path | None = None, runner_path: Path | None = None,
                 yolo_python_path: Path | None = None, yolo_model_path: Path | None = None,
                 yolo_runner_path: Path | None = None, tasks_root: Path | None = None) -> None:
        self.storage_dir = storage_dir
        self.tasks_root = tasks_root if tasks_root is not None else BACKEND_ROOT / "user_data" / "tasks"
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
            configured_yolo = os.environ.get("WATER_ANALYSIS_YOLO_MODEL", "").strip()
            self.yolo_model_path = Path(configured_yolo) if configured_yolo else None
        else:
            self.yolo_model_path = _configured_path("WATER_ANALYSIS_YOLO_MODEL", DEFAULT_YOLO_WEIGHTS)
        self.yolo_runner_path = yolo_runner_path if yolo_runner_path is not None else YOLO_RUNNER
        self.remote_runner_path = REMOTE_RUNNER
        self.frame_resolver: FrameResolver | None = None
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._job_dirs: dict[str, Path] = {}
        self._runtime: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # models / health
    # ------------------------------------------------------------------
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
                          "需要配置 yolov8-env")
        return [
            {"id": ONNX_MODEL_ID, "label": "ABCDWaveNet 本地混合模型", "description": "语义分割 · RGB 积水候选",
             "available": onnx_ready, "message": onnx_message},
            {"id": YOLO_MODEL_ID, "label": "YOLOv8n-seg 4090 混训冠军", "description": "实例分割 · Puddle/FRED/FloodNet/本地种子混训",
             "available": yolo_ready, "message": yolo_message},
            {"id": REMOTE_MODEL_ID, "label": "本地视觉模型", "description": "视觉识别水坑候选；本机把返回的多边形栅格化为掩膜和叠图",
             "available": remote_ready, "message": remote_message},
        ]

    def health(self) -> dict[str, Any]:
        with self._lock:
            busy = any(job["state"] in ("pending", "running") for job in self._jobs.values())
        models = self._model_options()
        available = any(model["available"] for model in models)
        yolo_available = next((model["available"] for model in models if model["id"] == YOLO_MODEL_ID), False)
        default_model_id = YOLO_MODEL_ID if yolo_available else ONNX_MODEL_ID
        message = "照片分析组件已就绪。可选择本地分割模型或本地视觉模型。" if available else (
            "照片分析不可用：" + "；".join(model["message"] for model in models)
        )
        return {"available": available, "busy": busy, "models": models,
                "default_model_id": default_model_id,
                "python": str(self.python_path), "model_dir": str(self.model_dir),
                "max_photos_per_job": MAX_PHOTOS_PER_JOB, "message": message}

    def _select_model(self, model_id: str | None) -> dict[str, Any]:
        health = self.health()
        if not health["available"]:
            raise PhotoAnalysisError(health["message"], http_status=503)
        selected_id = model_id or health["default_model_id"]
        selected_model = next((model for model in health["models"] if model["id"] == selected_id), None)
        if selected_model is None:
            raise PhotoAnalysisError("无法识别所选照片分析模型。")
        if not selected_model["available"]:
            raise PhotoAnalysisError(f"所选模型未就绪：{selected_model['message']}", http_status=503)
        return selected_model

    def _claim_single_slot(self) -> None:
        with self._lock:
            if any(job["state"] in ("pending", "running") for job in self._jobs.values()):
                raise PhotoAnalysisError("已有分析任务在运行，请等待完成后再提交。", http_status=409)

    @staticmethod
    def _new_job_id() -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]

    def _job_dir_for(self, job_id: str, task_id: str | None) -> Path:
        return (self.tasks_root / task_id / "media" / "photo-analysis" / job_id
                if task_id else self.storage_dir / job_id)

    # ------------------------------------------------------------------
    # legacy whole-batch flow (kept for compatibility and tests)
    # ------------------------------------------------------------------
    def submit(self, photos: list[tuple[str, bytes]], context: dict[str, Any] | None = None,
               model_id: str | None = None, task_id: str | None = None) -> str:
        if not photos:
            raise PhotoAnalysisError("没有收到照片文件。")
        if len(photos) > MAX_PHOTOS_PER_JOB:
            raise PhotoAnalysisError(f"一次最多 {MAX_PHOTOS_PER_JOB} 张照片。")
        selected_model = self._select_model(model_id)
        self._claim_single_slot()
        with self._lock:
            job_id = self._new_job_id()
            job_dir = self._job_dir_for(job_id, task_id)
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
                "mode": "legacy",
                "created_at": _now_iso(),
                "finished_at": None,
                "context": context or {},
                "task_id": task_id,
                "model_id": selected_model["id"],
                "inputs": saved,
                "results": None,
                "error": None,
            }
            self._jobs[job_id] = job
            self._job_dirs[job_id] = job_dir
        thread = threading.Thread(target=self._process, args=(job_id,), daemon=True, name=f"photo-analysis-{job_id}")
        thread.start()
        return job_id

    def _process(self, job_id: str) -> None:
        job_dir = self._job_dirs[job_id]
        output_dir = job_dir / "outputs"
        job = self.job(job_id)
        environment = None
        if job["model_id"] == REMOTE_MODEL_ID:
            environment = self._remote_environment()
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
                raise RuntimeError(detail)
            with self._lock:
                self._jobs[job_id]["results"] = self._enrich_with_ground_scale(payload, job["context"])
                self._jobs[job_id]["state"] = "done"
                self._jobs[job_id]["finished_at"] = _now_iso()
        except Exception as error:  # noqa: BLE001 - stored on the job for the UI
            with self._lock:
                self._jobs[job_id]["state"] = "error"
                detail = self._scrub_secret(str(error))
                self._jobs[job_id]["error"] = f"{type(error).__name__}: {detail}"
                self._jobs[job_id]["finished_at"] = _now_iso()

    # ------------------------------------------------------------------
    # ground-scale enrichment
    # ------------------------------------------------------------------
    @staticmethod
    def _context_fov(context: dict[str, Any]) -> float:
        try:
            fov_deg = float(context.get("camera_fov_deg") or DEFAULT_CAMERA_FOV_DEGREES)
        except (TypeError, ValueError):
            fov_deg = DEFAULT_CAMERA_FOV_DEGREES
        return min(max(fov_deg, 30.0), 120.0)

    def _enrich_image(self, image: dict[str, Any], frame_meta: dict[str, Any], fov_deg: float) -> dict[str, Any]:
        """Attach ground-scale estimates for one frame (SRT altitude + camera FOV).

        Flat-ground, near-nadir assumption; oblique frames get a coarse
        1/cos^2 correction and are flagged so the UI can downgrade the alert.
        """
        altitude = frame_meta.get("altitude_m")
        pitch = frame_meta.get("gimbal_pitch_deg")
        width = int(image.get("width") or 0)
        height = int(image.get("height") or 0)
        image["altitude_m"] = float(altitude) if isinstance(altitude, (int, float)) and altitude > 0 else None
        image["gimbal_pitch_deg"] = float(pitch) if isinstance(pitch, (int, float)) else None
        image["camera_fov_deg"] = fov_deg
        image.update({"area_estimate": "none", "gsd_m": None, "ground_width_m": None,
                      "ground_height_m": None, "estimated_area_m2": None, "largest_patch_area_m2": None})
        if image["altitude_m"] is None or width <= 0 or height <= 0:
            return image
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
        return image

    def _enrich_with_ground_scale(self, payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        """Legacy batch enrichment: frame metadata matched by submission order."""
        frames = context.get("frames")
        frames = frames if isinstance(frames, list) else []
        fov_deg = self._context_fov(context)
        for index, image in enumerate(payload.get("images", [])):
            frame = frames[index] if index < len(frames) and isinstance(frames[index], dict) else {}
            self._enrich_image(image, frame, fov_deg)
        return payload

    @staticmethod
    def _scrub_secret(detail: str) -> str:
        secret = _configured_value("WATER_ANALYSIS_REMOTE_API_KEY")
        return detail.replace(secret, "[已隐藏 API Key]") if secret else detail

    # ------------------------------------------------------------------
    # frame-queue flow
    # ------------------------------------------------------------------
    def _remote_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update({
            "WATER_ANALYSIS_REMOTE_URL": _configured_value("WATER_ANALYSIS_REMOTE_URL"),
            "WATER_ANALYSIS_REMOTE_API_KEY": _configured_value("WATER_ANALYSIS_REMOTE_API_KEY"),
            "WATER_ANALYSIS_REMOTE_MODEL": _configured_value("WATER_ANALYSIS_REMOTE_MODEL", "glm-5.3-flash"),
        })
        return environment

    def _queue_command(self, model_id: str, manifest: Path, output_dir: Path) -> tuple[list[str], dict[str, str] | None]:
        if model_id == REMOTE_MODEL_ID:
            return ([str(self.yolo_python_path), "-B", str(self.remote_runner_path),
                     "--manifest", str(manifest), "--output-dir", str(output_dir)], self._remote_environment())
        if model_id == YOLO_MODEL_ID:
            return ([str(self.yolo_python_path), "-B", str(self.yolo_runner_path),
                     "--weights", str(self.yolo_model_path),
                     "--manifest", str(manifest), "--output-dir", str(output_dir)], None)
        return ([str(self.python_path), "-B", str(self.runner_path),
                 "--model-dir", str(self.model_dir),
                 "--manifest", str(manifest), "--output-dir", str(output_dir)], None)

    def _resolve_frames(self, task_id: str, frame_ids: list[str]) -> list[dict[str, Any]]:
        if self.frame_resolver is None:
            raise PhotoAnalysisError("任务关键帧解析器尚未配置。", http_status=503)
        resolved: list[dict[str, Any]] = []
        seen: set[str] = set()
        for frame_id in frame_ids:
            if frame_id in seen:
                continue
            seen.add(frame_id)
            try:
                info = self.frame_resolver(task_id, frame_id)
                path = Path(info["path"])
            except PhotoAnalysisError:
                raise
            except Exception as error:  # noqa: BLE001 - resolver errors are user-facing
                raise PhotoAnalysisError(f"无法解析任务关键帧 {frame_id}：{error}") from error
            if not path.is_file():
                raise PhotoAnalysisError(f"任务关键帧文件不存在：{info.get('name', frame_id)}")
            resolved.append({"frame_id": frame_id, "name": info["name"], "path": path,
                             "meta": info.get("meta") or {}})
        return resolved

    def submit_frames(self, task_id: str, frame_ids: list[str], model_id: str | None = None,
                      context: dict[str, Any] | None = None, streaming: bool = False) -> str:
        if not task_id:
            raise PhotoAnalysisError("队列分析需要绑定任务。")
        frame_ids = [item for item in frame_ids if isinstance(item, str) and item.strip()]
        if not frame_ids and not streaming:
            raise PhotoAnalysisError("没有收到要分析的关键帧。")
        if len(frame_ids) > MAX_PHOTOS_PER_JOB:
            raise PhotoAnalysisError(f"一次最多 {MAX_PHOTOS_PER_JOB} 张照片。")
        selected_model = self._select_model(model_id)
        resolved = self._resolve_frames(task_id, frame_ids)
        with self._lock:
            self._claim_single_slot_locked()
            job_id = self._new_job_id()
            job_dir = self._job_dir_for(job_id, task_id)
            (job_dir / "outputs").mkdir(parents=True, exist_ok=True)
            items = [{"frame_id": info["frame_id"], "name": info["name"], "file_name": info["path"].name,
                      "meta": info["meta"], "state": "pending", "error": None, "attempts": 0}
                     for info in resolved]
            job = {
                "job_id": job_id,
                "state": "running",
                "mode": "frames",
                "created_at": _now_iso(),
                "finished_at": None,
                "context": context or {},
                "task_id": task_id,
                "model_id": selected_model["id"],
                "items": items,
                "closed": not streaming and bool(items),
                "inputs": [],
                "results": None,
                "error": None,
            }
            self._jobs[job_id] = job
            self._job_dirs[job_id] = job_dir
        self._start_queue_runtime(job_id, initial=True)
        self._persist(job_id)
        return job_id

    def _start_queue_runtime(self, job_id: str, initial: bool,
                             seed_results: dict[str, dict[str, Any]] | None = None,
                             jsonl_offset: int = 0) -> None:
        """Create the worker runtime for a job; only call when no job is active.

        ``seed_results`` carries already-computed per-image results (retry or
        restart recovery) so finalize can never race ahead of the seeding.
        """
        with self._lock:
            job = self._jobs[job_id]
            if job["state"] not in ("pending", "running"):
                raise PhotoAnalysisError("该分析任务已结束，无法启动队列。", http_status=409)
            runtime: dict[str, Any] = {
                "queue": queue.Queue(),
                "stop": threading.Event(),
                "cancel_requested": False,
                "active_workers": QUEUE_WORKERS.get(job["model_id"], 1),
                "worker_lock": threading.Lock(),
                "jsonl_offset": jsonl_offset,
                "jsonl_lock": threading.Lock(),
                "results_map": dict(seed_results or {}),
                "success_count": len(seed_results or {}),
                "consec_failures": 0,
                "circuit_error": None,
                "started_mono": time.monotonic(),
                "last_activity": time.time(),
                "procs": set(),
                "batch_seq": 0,
                "model_block": None,
                "interpretation": None,
            }
            self._runtime[job_id] = runtime
            enqueue = []
            for index, item in enumerate(job["items"]):
                if item["state"] in ("pending", "running"):
                    if initial:
                        item["state"] = "pending"
                    enqueue.append(index)
            workers = runtime["active_workers"]
        for index in enqueue:
            runtime["queue"].put(index)
        for number in range(workers):
            threading.Thread(target=self._queue_worker, args=(job_id,), daemon=True,
                             name=f"photo-queue-{job_id}-{number}").start()

    def append_frames(self, job_id: str, frame_ids: list[str]) -> int:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise PhotoAnalysisError("找不到该分析任务。", http_status=404)
            if job.get("mode") != "frames":
                raise PhotoAnalysisError("该任务不支持追加关键帧。", http_status=409)
            if job["state"] != "running":
                raise PhotoAnalysisError("该分析任务已结束，无法追加关键帧。", http_status=409)
            if job["closed"]:
                raise PhotoAnalysisError("该分析任务已关闭输入，无法追加关键帧。", http_status=409)
            if len(job["items"]) + len(frame_ids) > MAX_PHOTOS_PER_JOB:
                raise PhotoAnalysisError(f"一个任务最多分析 {MAX_PHOTOS_PER_JOB} 张照片。", http_status=409)
        resolved = self._resolve_frames(job["task_id"], frame_ids)
        runtime = self._runtime.get(job_id)
        if runtime is None:
            raise PhotoAnalysisError("分析队列未在运行。", http_status=409)
        appended: list[int] = []
        with self._lock:
            existing = {item["frame_id"] for item in job["items"]}
            for info in resolved:
                if info["frame_id"] in existing:
                    continue
                existing.add(info["frame_id"])
                job["items"].append({"frame_id": info["frame_id"], "name": info["name"],
                                     "file_name": info["path"].name, "meta": info["meta"],
                                     "state": "pending", "error": None, "attempts": 0})
                appended.append(len(job["items"]) - 1)
            runtime["last_activity"] = time.time()
        for index in appended:
            runtime["queue"].put(index)
        self._persist(job_id)
        return len(appended)

    def close_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise PhotoAnalysisError("找不到该分析任务。", http_status=404)
            if job.get("mode") != "frames":
                raise PhotoAnalysisError("该任务不支持关闭输入。", http_status=409)
            if job["state"] != "running":
                return
            job["closed"] = True
            runtime = self._runtime.get(job_id)
            if runtime is not None:
                runtime["last_activity"] = time.time()
        self._persist(job_id)

    def cancel_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise PhotoAnalysisError("找不到该分析任务。", http_status=404)
            if job.get("mode") != "frames":
                raise PhotoAnalysisError("该任务不支持取消操作。", http_status=409)
            if job["state"] != "running":
                return
            runtime = self._runtime.get(job_id)
            if runtime is None:
                return
            runtime["cancel_requested"] = True
            runtime["stop"].set()
            procs = list(runtime["procs"])
        for proc in procs:
            try:
                proc.terminate()
            except OSError:
                pass
        self._persist(job_id)

    def retry_failed(self, job_id: str) -> int:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise PhotoAnalysisError("找不到该分析任务。", http_status=404)
            if job.get("mode") != "frames":
                raise PhotoAnalysisError("该任务不支持重试失败帧。", http_status=409)
            if job["state"] not in ("done", "error", "cancelled"):
                raise PhotoAnalysisError("任务仍在运行，无法重试失败帧。", http_status=409)
            failed = [item for item in job["items"] if item["state"] == "failed"]
            if not failed:
                raise PhotoAnalysisError("没有可重试的失败帧。", http_status=409)
            self._claim_single_slot_locked()
            task_dir = self._job_dirs[job_id].parents[2]
            for item in failed:
                path = task_dir / "frames" / item["file_name"]
                if not path.is_file():
                    item["state"] = "failed"
                    item["error"] = "关键帧文件已不存在，无法重试。"
                    continue
                item["state"] = "pending"
                item["error"] = None
                item["attempts"] = 0
            job["state"] = "running"
            job["error"] = None
            job["finished_at"] = None
            job["closed"] = True
            previous = job.get("results") or {}
            previous_images = [image for image in (previous.get("images") or []) if isinstance(image, dict) and image.get("name")]
        seed = {image["name"]: image for image in previous_images}
        self._start_queue_runtime(job_id, initial=False, seed_results=seed)
        self._persist(job_id)
        return len(failed)

    def _claim_single_slot_locked(self) -> None:
        # caller already holds self._lock
        if any(job["state"] in ("pending", "running") for job in self._jobs.values()):
            raise PhotoAnalysisError("已有分析任务在运行，请等待完成后再提交。", http_status=409)

    # -- queue workers --------------------------------------------------
    def _queue_worker(self, job_id: str) -> None:
        runtime = self._runtime.get(job_id)
        if runtime is None:
            return
        while not runtime["stop"].is_set():
            try:
                first = runtime["queue"].get(timeout=0.5)
            except queue.Empty:
                self._maybe_autoclose(job_id)
                with self._lock:
                    job = self._jobs.get(job_id)
                    if job is not None and job["closed"] and self._all_items_terminal(job):
                        break
                continue
            batch = [first]
            while len(batch) < QUEUE_BATCH_SIZE:
                try:
                    batch.append(runtime["queue"].get_nowait())
                except queue.Empty:
                    break
            if runtime["stop"].is_set():
                break
            self._run_queue_batch(job_id, batch)
        with runtime["worker_lock"]:
            runtime["active_workers"] -= 1
            if runtime["active_workers"] <= 0:
                self._finalize_queue_job(job_id)

    @staticmethod
    def _all_items_terminal(job: dict[str, Any]) -> bool:
        return all(item["state"] in ("done", "failed", "cancelled") for item in job.get("items", []))

    def _maybe_autoclose(self, job_id: str) -> None:
        runtime = self._runtime.get(job_id)
        if runtime is None:
            return
        if time.time() - runtime["last_activity"] < QUEUE_IDLE_AUTOCLOSE_SECONDS:
            return
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job["closed"] or job["state"] != "running":
                return
            if time.time() - runtime["last_activity"] < QUEUE_IDLE_AUTOCLOSE_SECONDS:
                return
            job["closed"] = True
            job["error"] = job["error"] or "长时间没有新的关键帧入队，已自动结束该分析任务。"
        self._persist(job_id)

    def _run_queue_batch(self, job_id: str, indices: list[int]) -> None:
        runtime = self._runtime[job_id]
        job_dir = self._job_dirs[job_id]
        output_dir = job_dir / "outputs"
        with self._lock:
            job = self._jobs[job_id]
            model_id = job["model_id"]
            fov_deg = self._context_fov(job["context"])
            for index in indices:
                job["items"][index]["state"] = "running"
            frame_names = [job["items"][index]["file_name"] for index in indices]
            runtime["batch_seq"] += 1
            batch_number = runtime["batch_seq"]
        batches_dir = job_dir / "batches"
        batches_dir.mkdir(parents=True, exist_ok=True)
        manifest = batches_dir / f"batch-{batch_number:04d}.json"
        frames_dir = job_dir.parents[2] / "frames"
        manifest.write_text(json.dumps([str(frames_dir / name) for name in frame_names], ensure_ascii=False),
                            encoding="utf-8")
        command, environment = self._queue_command(model_id, manifest, output_dir)
        timeout = QUEUE_BATCH_TIMEOUT_BASE[model_id] + QUEUE_BATCH_TIMEOUT_PER_IMAGE[model_id] * len(indices)
        timed_out = False
        proc = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        runtime["procs"].add(proc)
        try:
            proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            proc.communicate()
        finally:
            runtime["procs"].discard(proc)
        records = self._read_new_jsonl(job_id)
        retries: list[tuple[int, str]] = []
        with self._lock:
            job = self._jobs[job_id]
            for index in indices:
                item = job["items"][index]
                record = records.get(item["name"])
                if record is not None and "error" not in record:
                    enriched = self._enrich_image(dict(record), item.get("meta") or {}, fov_deg)
                    item["state"] = "done"
                    item["error"] = None
                    runtime["results_map"][item["name"]] = enriched
                    runtime["success_count"] += 1
                    runtime["consec_failures"] = 0
                    continue
                if record is not None and record.get("error"):
                    detail = str(record["error"])[:600]
                elif timed_out:
                    detail = "该批识别超时，未产出结果。"
                elif proc.returncode not in (0, None):
                    detail = "识别进程异常退出，未产出结果。"
                else:
                    detail = "该帧未产出识别结果。"
                detail = self._scrub_secret(detail)
                item["attempts"] += 1
                item["error"] = detail
                if item["attempts"] < QUEUE_MAX_ATTEMPTS and not runtime["stop"].is_set():
                    item["state"] = "pending"
                    retries.append((index, QUEUE_RETRY_DELAYS[min(item["attempts"] - 1, len(QUEUE_RETRY_DELAYS) - 1)]))
                else:
                    item["state"] = "failed"
                    runtime["consec_failures"] += 1
            if (runtime["consec_failures"] >= QUEUE_CIRCUIT_FAILURES and runtime["success_count"] == 0
                    and runtime["circuit_error"] is None):
                last_error = next((item["error"] for item in reversed(job["items"]) if item["state"] == "failed"), "未知错误")
                runtime["circuit_error"] = (
                    f"连续 {QUEUE_CIRCUIT_FAILURES} 帧识别失败且无一成功，判定为系统性故障，已自动中止剩余帧：{last_error}")
                runtime["stop"].set()
            if runtime["model_block"] is None:
                self._absorb_runner_metadata(job_id, runtime)
            runtime["last_activity"] = time.time()
        self._persist(job_id)
        for index, delay in retries:
            time.sleep(delay)
            if runtime["stop"].is_set():
                break
            runtime["queue"].put(index)

    def _absorb_runner_metadata(self, job_id: str, runtime: dict[str, Any]) -> None:
        try:
            summary = json.loads((self._job_dirs[job_id] / "outputs" / "results.json").read_text(encoding="utf-8"))
            if isinstance(summary.get("model"), dict):
                runtime["model_block"] = summary["model"]
            if isinstance(summary.get("interpretation"), str):
                runtime["interpretation"] = summary["interpretation"]
        except (OSError, ValueError, TypeError):
            pass

    def _read_new_jsonl(self, job_id: str) -> dict[str, dict[str, Any]]:
        runtime = self._runtime[job_id]
        path = self._job_dirs[job_id] / "outputs" / "results.jsonl"
        with runtime["jsonl_lock"]:
            try:
                data = path.read_bytes()
            except OSError:
                return {}
            if len(data) <= runtime["jsonl_offset"]:
                return {}
            fresh = data[runtime["jsonl_offset"]:]
            last_newline = fresh.rfind(b"\n")
            if last_newline < 0:
                return {}
            runtime["jsonl_offset"] += last_newline + 1
            consumed = fresh[:last_newline + 1]
        records: dict[str, dict[str, Any]] = {}
        for line in consumed.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            name = record.get("name")
            if isinstance(name, str):
                records[name] = record
        return records

    def _finalize_queue_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            runtime = self._runtime.get(job_id)
            if job is None or runtime is None or job["state"] != "running":
                return
            for item in job["items"]:
                if item["state"] not in ("done", "failed"):
                    item["state"] = "cancelled"
            if runtime["circuit_error"]:
                job["state"] = "error"
                job["error"] = self._scrub_secret(runtime["circuit_error"])
            elif runtime["cancel_requested"]:
                job["state"] = "cancelled"
            else:
                job["state"] = "done"
            job["finished_at"] = _now_iso()
            images = [runtime["results_map"][item["name"]] for item in job["items"]
                      if item["state"] == "done" and item["name"] in runtime["results_map"]]
            job["results"] = {
                "model": runtime.get("model_block") or {"default_model": job["model_id"], "engine": "queue"},
                "images": images,
                "elapsed_seconds": round(time.monotonic() - runtime["started_mono"], 2),
                "interpretation": runtime.get("interpretation") or FALLBACK_INTERPRETATION,
            }
            self._runtime.pop(job_id, None)
        self._persist(job_id)

    # ------------------------------------------------------------------
    # persistence / recovery
    # ------------------------------------------------------------------
    def _persist(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.get("mode") != "frames":
                return
            payload = {key: job.get(key) for key in
                       ("job_id", "state", "mode", "task_id", "model_id", "context",
                        "closed", "created_at", "finished_at", "error")}
            payload["items"] = json.loads(json.dumps(job["items"], ensure_ascii=False))
        try:
            _atomic_write(self._job_dirs[job_id] / "job.json",
                          json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        except OSError:
            pass

    def recover_jobs(self) -> int:
        """Resume non-terminal frame-queue jobs after a backend restart.

        Only the newest interrupted job is resumed; older ones are marked
        cancelled so a single active job is guaranteed.
        """
        base = self.tasks_root
        if not base.is_dir():
            return 0
        candidates: list[tuple[str, str, Path, dict[str, Any]]] = []
        for job_json in base.glob("*/media/photo-analysis/*/job.json"):
            try:
                data = json.loads(job_json.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if data.get("mode") != "frames" or data.get("state") not in ("pending", "running"):
                continue
            job_id = data.get("job_id") or job_json.parent.name
            candidates.append((data.get("created_at") or "", job_id, job_json.parent, data))
        candidates.sort()
        resumed = 0
        for created_at, job_id, job_dir, data in candidates:
            task_id = job_dir.parents[2].name
            if resumed > 0:
                data["state"] = "cancelled"
                data["error"] = "后端重启导致任务中断；可对失败帧重试。"
                data["finished_at"] = _now_iso()
                try:
                    _atomic_write(job_dir / "job.json", json.dumps(data, ensure_ascii=False, indent=2) + "\n")
                except OSError:
                    pass
                continue
            resumed += self._resume_job(job_id, task_id, job_dir, data)
        return resumed

    def _resume_job(self, job_id: str, task_id: str, job_dir: Path, data: dict[str, Any]) -> int:
        items = [item for item in data.get("items", []) if isinstance(item, dict)]
        if not items:
            return 0
        task_dir = job_dir.parents[2]
        for item in items:
            path = task_dir / "frames" / str(item.get("file_name") or "")
            if item.get("state") != "done" and not path.is_file():
                item["state"] = "failed"
                item["error"] = "后端重启后关键帧文件缺失，无法续跑。"
        # 先重放已产出的 results.jsonl，已完成帧直接恢复，不再排队识别。
        records: dict[str, dict[str, Any]] = {}
        jsonl_path = job_dir / "outputs" / "results.jsonl"
        jsonl_size = 0
        if jsonl_path.is_file():
            try:
                raw = jsonl_path.read_bytes()
                jsonl_size = len(raw)
                for line in raw.decode("utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(record.get("name"), str):
                        records[record["name"]] = record
            except OSError:
                pass
        context = data.get("context") or {}
        fov_deg = self._context_fov(context)
        seed: dict[str, dict[str, Any]] = {}
        for item in items:
            record = records.get(item.get("name"))
            if item.get("state") in ("pending", "running") and record is not None and "error" not in record:
                item["state"] = "done"
                seed[item["name"]] = self._enrich_image(dict(record), item.get("meta") or {}, fov_deg)
        job = {
            "job_id": job_id,
            "state": "running",
            "mode": "frames",
            "created_at": data.get("created_at") or _now_iso(),
            "finished_at": None,
            "context": context,
            "task_id": task_id,
            "model_id": data.get("model_id") or ONNX_MODEL_ID,
            "items": items,
            "closed": bool(data.get("closed")),
            "inputs": [],
            "results": None,
            "error": data.get("error"),
        }
        with self._lock:
            if any(existing["state"] in ("pending", "running") for existing in self._jobs.values()):
                return 0
            self._jobs[job_id] = job
            self._job_dirs[job_id] = job_dir
        self._start_queue_runtime(job_id, initial=True, seed_results=seed, jsonl_offset=jsonl_size)
        runtime = self._runtime.get(job_id)
        if runtime is not None:
            self._absorb_runner_metadata(job_id, runtime)
            self._persist(job_id)
        return 1

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    def _fallback_model_block(self, model_id: str) -> dict[str, Any]:
        blocks = {
            REMOTE_MODEL_ID: {"default_model": model_id, "engine": "remote-vision-polygon-mask"},
            YOLO_MODEL_ID: {"default_model": model_id, "engine": "Ultralytics"},
            ONNX_MODEL_ID: {"default_model": model_id, "engine": "onnx"},
        }
        return blocks.get(model_id, {"default_model": model_id})

    def job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise PhotoAnalysisError("找不到该分析任务。", http_status=404)
            snapshot = json.loads(json.dumps(job, ensure_ascii=False))
            runtime = self._runtime.get(job_id)
            if job.get("mode") == "frames":
                counts = {"total": len(snapshot.get("items", [])), "done": 0, "failed": 0,
                          "pending": 0, "running": 0, "cancelled": 0}
                for item in snapshot.get("items", []):
                    counts[item["state"]] = counts.get(item["state"], 0) + 1
                snapshot["counts"] = counts
                if runtime is not None and snapshot["state"] in ("pending", "running"):
                    images = [runtime["results_map"][item["name"]] for item in snapshot.get("items", [])
                              if item["state"] == "done" and item["name"] in runtime["results_map"]]
                    snapshot["results"] = {
                        "model": runtime.get("model_block") or self._fallback_model_block(snapshot["model_id"]),
                        "images": images,
                        "elapsed_seconds": round(time.monotonic() - runtime["started_mono"], 2),
                        "interpretation": runtime.get("interpretation") or FALLBACK_INTERPRETATION,
                    }
            return snapshot

    def output_file(self, job_id: str, name: str) -> Path:
        job = self.job(job_id)
        if job.get("mode") == "frames":
            allowed: dict[str, Path] = {}
            outputs = self._job_dirs[job_id] / "outputs"
            for image in (job.get("results") or {}).get("images", []):
                for key in ("overlay", "suspected_mask", "confidence_map"):
                    artifact = image.get(key)
                    if artifact:
                        allowed[Path(artifact).name] = outputs / artifact
            relative = allowed.get(name) or allowed.get(Path(name).name)
            if relative is None:
                raise PhotoAnalysisError("不允许的文件名。", http_status=404)
            target = relative.resolve()
            outputs_root = outputs.resolve()
            if outputs_root != target and outputs_root not in target.parents:
                raise PhotoAnalysisError("不允许的文件名。", http_status=404)
            if not target.is_file():
                raise PhotoAnalysisError("找不到文件。", http_status=404)
            return target
        if job["state"] != "done":
            raise PhotoAnalysisError("任务尚未完成。", http_status=409)
        allowed_legacy: dict[str, str] = {}
        for image in job["results"]["images"]:
            for key in ("overlay", "suspected_mask", "confidence_map"):
                if image.get(key):
                    allowed_legacy[Path(image[key]).name] = f"outputs/{image[key]}"
            allowed_legacy[image["name"]] = f"inputs/{image['name']}"
        relative = allowed_legacy.get(name) or allowed_legacy.get(Path(name).name)
        if relative is None:
            raise PhotoAnalysisError("不允许的文件名。", http_status=404)
        base = self._job_dirs.get(job_id, self.storage_dir / job_id)
        target = (base / relative).resolve()
        if base.resolve() not in target.parents or not target.is_file():
            raise PhotoAnalysisError("找不到文件。", http_status=404)
        return target


photo_analysis = PhotoAnalysisManager()

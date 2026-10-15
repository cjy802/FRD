"""Post-flight photo analysis jobs: uploaded drone photos -> ONNX water segmentation.

The web backend never imports the ML stack. Uploaded photos are stored under
user_data/photo_analysis/<job id>/inputs and processed by scripts/run_water_onnx.py
inside the waterseg conda environment (configured via WATER_ANALYSIS_PYTHON),
mirroring the FuXi subprocess pattern in live_weather.py. Results (overlay PNGs,
suspected masks, confidence maps, results.json) stay in the job directory and are
served back through the API. Interpretation follows the waterwatch discipline:
suspected pixels are leads for manual review, never depth/water-level conclusions.
"""

from __future__ import annotations

import json
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
RUNNER = BACKEND_ROOT / "scripts" / "run_water_onnx.py"
MAX_PHOTOS_PER_JOB = 200
MAX_PHOTO_BYTES = 40 * 1024 * 1024
ALLOWED_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
JOB_TIMEOUT_SECONDS = 900


class PhotoAnalysisError(Exception):
    def __init__(self, message: str, http_status: int = 400) -> None:
        super().__init__(message)
        self.http_status = http_status


def _configured_path(env_name: str, default: Path) -> Path:
    import os

    value = os.environ.get(env_name, "").strip()
    return Path(value) if value else default


class PhotoAnalysisManager:
    def __init__(self, *, storage_dir: Path = DEFAULT_STORAGE, python_path: Path | None = None,
                 model_dir: Path | None = None, runner_path: Path | None = None) -> None:
        self.storage_dir = storage_dir
        self.python_path = python_path if python_path is not None else _configured_path("WATER_ANALYSIS_PYTHON", DEFAULT_PYTHON)
        self.model_dir = model_dir if model_dir is not None else _configured_path("WATER_ANALYSIS_MODEL_DIR", DEFAULT_MODEL_DIR)
        self.runner_path = runner_path if runner_path is not None else RUNNER
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}

    def health(self) -> dict[str, Any]:
        with self._lock:
            busy = any(job["state"] in ("pending", "running") for job in self._jobs.values())
        python_ok = self.python_path.is_file()
        registry = self.model_dir / "registry.json"
        model_ok = registry.is_file()
        runner_ok = self.runner_path.is_file()
        message = (
            "照片分析组件已就绪。"
            if python_ok and model_ok and runner_ok
            else "照片分析不可用："
            + "；".join(part for part in (
                None if python_ok else f"缺少推理 Python（{self.python_path}）",
                None if model_ok else f"缺少模型 registry（{self.model_dir}）；先运行 waterwatch 导出 ONNX",
                None if runner_ok else "缺少 scripts/run_water_onnx.py",
            ) if part)
        )
        return {"available": python_ok and model_ok and runner_ok, "busy": busy,
                "python": str(self.python_path), "model_dir": str(self.model_dir),
                "max_photos_per_job": MAX_PHOTOS_PER_JOB, "message": message}
    def submit(self, photos: list[tuple[str, bytes]], context: dict[str, Any] | None = None) -> str:
        if not photos:
            raise PhotoAnalysisError("没有收到照片文件。")
        if len(photos) > MAX_PHOTOS_PER_JOB:
            raise PhotoAnalysisError(f"一次最多 {MAX_PHOTOS_PER_JOB} 张照片。")
        health = self.health()
        if not health["available"]:
            raise PhotoAnalysisError(health["message"], http_status=503)
        with self._lock:
            if any(job["state"] in ("pending", "running") for job in self._jobs.values()):
                raise PhotoAnalysisError("已有分析任务在运行，请等待完成后再提交。", http_status=409)
            job_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
            job_dir = self.storage_dir / job_id
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
                "inputs": saved,
                "results": None,
                "error": None,
            }
            self._jobs[job_id] = job
        thread = threading.Thread(target=self._process, args=(job_id,), daemon=True, name=f"photo-analysis-{job_id}")
        thread.start()
        return job_id

    def _process(self, job_id: str) -> None:
        job_dir = self.storage_dir / job_id
        output_dir = job_dir / "outputs"
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
                timeout=JOB_TIMEOUT_SECONDS, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
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
                self._jobs[job_id]["results"] = payload
                self._jobs[job_id]["state"] = "done"
                self._jobs[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()
        except Exception as error:  # noqa: BLE001 - stored on the job for the UI
            with self._lock:
                self._jobs[job_id]["state"] = "error"
                self._jobs[job_id]["error"] = f"{type(error).__name__}: {error}"
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
        base = self.storage_dir / job_id
        target = (base / relative).resolve()
        if base.resolve() not in target.parents or not target.is_file():
            raise PhotoAnalysisError("找不到文件。", http_status=404)
        return target


photo_analysis = PhotoAnalysisManager()

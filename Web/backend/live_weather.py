"""One on-entry forecast job, requiring a fresh prepared FuXi initial field."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from weather_service import (
    DEFAULT_MODEL, PROJECT_ROOT, WeatherUnavailable, assemble_forecast,
    configured_path, iso_utc, load_corrector, utc_timestamp,
)
from regional_weather import RegionalWeather
from route_weather import align_route


def validate_metadata(metadata: dict, now: datetime) -> tuple[datetime, datetime]:
    init = utc_timestamp(metadata["init_time_utc"])
    previous = utc_timestamp(metadata["previous_time_utc"])
    valid = utc_timestamp(metadata["valid_time_utc"])
    if init - previous != timedelta(hours=6) or valid - init != timedelta(hours=6) or metadata["lead_hours"] != 6:
        raise WeatherUnavailable("起报场与 +6 小时预报时间不一致。")
    if init > now:
        raise WeatherUnavailable("起报场时间晚于当前时间，不能作为已获得的输入。")
    if valid <= now:
        raise WeatherUnavailable(f"起报场已过期（起报 {iso_utc(init)}）；需要最新全球起报场才能预测未来天气。")
    if (float(metadata["latitude_deg"]), float(metadata["longitude_deg"])) != (31.5, 119.5):
        raise WeatherUnavailable("当前订正模型仅用于 31.5°N、119.5°E 点位。")
    return init, valid


def call_worker(input_path: Path, inspect: bool) -> dict:
    python = configured_path("FUXI_PYTHON", Path("D:/anaconda/envs/fuxi_onnx/python.exe"))
    command = [str(python), "-B", str(PROJECT_ROOT / "app/predict_fuxi_point.py"), "--input", str(input_path)]
    if inspect:
        command.append("--inspect")
    else:
        model_dir = configured_path("FUXI_MODEL_DIR", PROJECT_ROOT / "FuXi_EC")
        command.extend(["--model-dir", str(model_dir)])
    completed = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30 if inspect else 600,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        payload = json.loads(completed.stdout)
    except ValueError as error:
        raise WeatherUnavailable("FuXi 推理进程未返回有效结果，请检查 Python 环境及起报场文件。") from error
    if completed.returncode or "error" in payload:
        raise WeatherUnavailable(payload.get("error", "FuXi 推理失败。"))
    return payload


def prepare_latest_input(output: Path) -> Path:
    python = configured_path("FUXI_PYTHON", Path("D:/anaconda/envs/fuxi_onnx/python.exe"))
    completed = subprocess.run(
        [str(python), "-B", str(PROJECT_ROOT / "app/prepare_gfs_live.py"), "--output", str(output)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1200,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        payload = json.loads(completed.stdout)
    except ValueError as error:
        raise WeatherUnavailable("公开实时气象数据获取失败，请检查网络和 GFS 解码环境。") from error
    if completed.returncode or "error" in payload:
        raise WeatherUnavailable(f"自动获取当前天气失败：{payload.get('error', 'NOAA 数据源暂不可用')}")
    return Path(payload["input_path"])


class ForecastJobs:
    def __init__(self, worker=call_worker, clock=None, corrector=load_corrector, preparer=prepare_latest_input):
        self.worker = worker
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.corrector = corrector
        self.preparer = preparer
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fuxi-live")
        self.lock = threading.Lock()
        self.jobs: dict[str, dict] = {}
        self.grids: dict[str, RegionalWeather] = {}
        self.models: dict[str, dict] = {}
        self.active: str | None = None

    def get(self, job_id: str) -> dict:
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError(job_id)
            return copy.deepcopy(self.jobs[job_id])

    def update(self, job_id: str, **values):
        with self.lock:
            self.jobs[job_id].update(values)
            if values.get("status") in ("blocked", "failed", "succeeded"):
                if self.active == job_id:
                    self.active = None

    def regional_job(self, job_id: str):
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError(job_id)
            if self.jobs[job_id]["status"] != "succeeded" or job_id not in self.grids:
                raise WeatherUnavailable("区域天气尚未就绪，请等待预测完成。")
            grid, model = self.grids[job_id], self.models[job_id]
            generated = self.jobs[job_id]["forecast"]["generated_at_utc"]
        return grid, model, generated

    def sample(self, job_id: str, latitude: float, longitude: float, valid_time_utc: str | None = None) -> dict:
        grid, model, generated = self.regional_job(job_id)
        result = grid.sample(latitude, longitude, model, valid_time_utc)
        if utc_timestamp(result["valid_time_utc"]) <= self.clock():
            raise WeatherUnavailable("区域预报已过期，请重新预测。")
        result["generated_at_utc"] = generated
        return result

    def route(self, job_id: str, coordinates, departure_time_utc: str, speed_kmh: float, altitude_m: float):
        grid, model, generated = self.regional_job(job_id)
        result = align_route(grid, coordinates, departure_time_utc, speed_kmh, altitude_m, model, now=self.clock())
        return {**result, 'job_id': job_id, 'generated_at_utc': generated}

    def start(self) -> dict:
        with self.lock:
            if self.active:
                return {**copy.deepcopy(self.jobs[self.active]), "shared_in_progress": True}
            job_id = uuid.uuid4().hex
            self.jobs[job_id] = {
                "job_id": job_id, "status": "queued", "message": "正在检查最新全球起报场。",
                "requested_at_utc": iso_utc(self.clock()), "forecast": None,
            }
            self.active = job_id
            for old_id in list(self.jobs)[:-8]:
                self.jobs.pop(old_id, None)
                self.grids.pop(old_id, None)
                self.models.pop(old_id, None)
        self.executor.submit(self.run, job_id)
        return self.get(job_id)

    def run(self, job_id: str):
        try:
            input_path = configured_path("FUXI_LIVE_INPUT", PROJECT_ROOT / "app/prepared/latest.nc")
            if not os.getenv("FUXI_LIVE_INPUT", "").strip():
                self.update(job_id, status="running", message="正在自动获取 NOAA 当前天气资料；首次下载可能需要几分钟。")
                input_path = self.preparer(input_path)
            if not input_path.is_file():
                raise WeatherUnavailable("当前气象数据未获取成功，请稍后重新预测。")
            self.update(job_id, status="running", message="正在校验起报场时刻和网格。")
            metadata = self.worker(input_path, True)
            validate_metadata(metadata, self.clock())
            model = self.corrector(configured_path("WEATHER_CORRECTOR_PATH", DEFAULT_MODEL))
            self.update(job_id, message="FuXi 正在生成未来 6/12/18/24 小时区域天气。")
            predicted = self.worker(input_path, False)
            init, valid = validate_metadata(predicted, self.clock())
            if any(predicted[name] != metadata[name] for name in (
                "init_time_utc", "valid_time_utc", "previous_time_utc", "lead_hours",
                "latitude_deg", "longitude_deg",
            )):
                raise WeatherUnavailable("推理期间起报场发生变化，请重新预测。")
            row = {**predicted, "init": init, "valid": valid}
            forecast = assemble_forecast(row, model, historical=False)
            forecast["input_source"] = predicted.get("input_source", "Configured weather initial field")
            forecast["generated_at_utc"] = iso_utc(self.clock())
            if "region" in predicted:
                grid = RegionalWeather(predicted["region"], predicted["init_time_utc"], forecast["input_source"])
                with self.lock:
                    self.grids[job_id] = grid
                    self.models[job_id] = model
                forecast["regional_coverage"] = grid.coverage()
            self.update(job_id, status="succeeded", message="未来天气预报已生成。", forecast=forecast)
        except (WeatherUnavailable, FileNotFoundError) as error:
            self.update(job_id, status="blocked", message=str(error), forecast=None)
        except Exception as error:
            self.update(job_id, status="failed", message=f"天气预测失败：{error}", forecast=None)


forecast_jobs = ForecastJobs()

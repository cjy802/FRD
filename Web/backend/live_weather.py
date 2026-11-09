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
    PROJECT_ROOT, WeatherUnavailable, assemble_forecast,
    configured_path, default_corrector_path, iso_utc, load_corrector, point_model, point_risk, utc_timestamp,
)
from regional_weather import RegionalWeather
from route_weather import align_route
from height_weather import HeightWeather
from weather_risk import screen_route
from weather_decision import build_decision


MAX_CACHE_AGE = timedelta(hours=6)
DEFAULT_CACHE_PATH = PROJECT_ROOT / "Web" / "backend" / "user_data" / "weather_cache" / "latest.json"


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


def prepare_height_weather(input_path: Path) -> dict:
    python = configured_path('FUXI_PYTHON', Path('D:/anaconda/envs/fuxi_onnx/python.exe'))
    try:
        completed = subprocess.run(
            [str(python), '-X', 'faulthandler', '-B', str(PROJECT_ROOT / 'app/prepare_gfs_height.py'), '--input', str(input_path)],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=600,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
    except subprocess.TimeoutExpired as error:
        raise WeatherUnavailable('高度天气获取超过 600 秒，请检查 NOAA 连接后重新预测。') from error
    diagnostics = f'退出码 {completed.returncode} (0x{completed.returncode & 0xffffffff:08X})'
    if completed.stderr.strip():
        diagnostics += f"；错误输出：{completed.stderr.strip()[-1800:]}"
    try: payload = json.loads(completed.stdout)
    except ValueError as error:
        raise WeatherUnavailable(f'近地高度天气进程未返回有效 JSON；{diagnostics}') from error
    if not isinstance(payload, dict):
        raise WeatherUnavailable(f'高度天气进程返回的数据结构无效；{diagnostics}')
    if completed.returncode or 'error' in payload:
        raise WeatherUnavailable(f"高度天气获取失败：{payload.get('error', 'NOAA 辅助资料不可用')}；{diagnostics}")
    return payload


class ForecastJobs:
    def __init__(self, worker=call_worker, clock=None, corrector=load_corrector, preparer=prepare_latest_input,
                 height_provider=prepare_height_weather, cache_path: Path | None = None):
        self.worker = worker
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.corrector = corrector
        self.preparer = preparer
        self.height_provider = height_provider
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fuxi-live")
        self.lock = threading.Lock()
        self.jobs: dict[str, dict] = {}
        self.grids: dict[str, RegionalWeather] = {}
        self.models: dict[str, dict] = {}
        self.height_grids: dict[str, HeightWeather] = {}
        self.height_warnings: dict[str, str] = {}
        self.active: str | None = None
        self.cache_path = cache_path
        self.latest_successful_job_id: str | None = None
        if self.cache_path is not None:
            self._restore_cache()

    def get(self, job_id: str) -> dict:
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError(job_id)
            return copy.deepcopy(self.jobs[job_id])

    def update(self, job_id: str, **values):
        with self.lock:
            self.jobs[job_id].update(values)
            if values.get("status") == "succeeded":
                self.latest_successful_job_id = job_id
            if values.get("status") in ("blocked", "failed", "succeeded"):
                if self.active == job_id:
                    self.active = None

    @staticmethod
    def _serialize_grid(grid: RegionalWeather) -> dict:
        payload = {
            "init_time_utc": iso_utc(grid.init),
            "input_source": grid.input_source,
            "input_frames": grid.input_frames,
            "latitudes": grid.latitudes.tolist(),
            "longitudes": grid.longitudes.tolist(),
            "channels": grid.channels,
            "snapshots": [{
                "valid_time_utc": iso_utc(item["valid"]),
                "lead_hours": item["lead_hours"],
                "source": item["source"],
                "values": item["values"].tolist(),
            } for item in grid.snapshots],
        }
        if isinstance(grid, HeightWeather):
            payload.update({"height_reference": "AGL", "heights_m": [10, 20, 30, 40, 50, 80, 100],
                            "source_cycle_utc": iso_utc(grid.source_cycle)})
        return payload

    @staticmethod
    def _restore_grid(payload: dict, *, height: bool = False) -> RegionalWeather:
        if height:
            return HeightWeather(payload, payload["init_time_utc"])
        return RegionalWeather(payload, payload["init_time_utc"], payload["input_source"],
                               input_frames=payload.get("input_frames"))

    def _restore_cache(self) -> None:
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            job = payload["job"]
            job_id = job["job_id"]
            if payload.get("version") != 1 or job.get("status") != "succeeded" or not job.get("forecast"):
                return
            generated = utc_timestamp(job["forecast"]["generated_at_utc"])
            valid = utc_timestamp(job["forecast"]["valid_time_utc"])
            now = self.clock()
            if now - generated >= MAX_CACHE_AGE or valid <= now:
                return
            grid = self._restore_grid(payload["grid"])
            height_payload = payload.get("height_grid")
            height_grid = self._restore_grid(height_payload, height=True) if height_payload else None
            model = self.corrector(configured_path("WEATHER_CORRECTOR_PATH", default_corrector_path()))
            with self.lock:
                self.jobs[job_id] = job
                self.grids[job_id] = grid
                self.models[job_id] = model
                self.latest_successful_job_id = job_id
                if height_grid is not None:
                    self.height_grids[job_id] = height_grid
                if payload.get("height_warning"):
                    self.height_warnings[job_id] = payload["height_warning"]
        except (OSError, KeyError, TypeError, ValueError, WeatherUnavailable):
            # An incomplete or incompatible local cache should never prevent a fresh prediction.
            return

    def _persist_cache(self, job_id: str) -> None:
        if self.cache_path is None:
            return
        with self.lock:
            job = copy.deepcopy(self.jobs[job_id])
            grid = self.grids.get(job_id)
            height_grid = self.height_grids.get(job_id)
            height_warning = self.height_warnings.get(job_id)
        if grid is None or job.get("status") != "succeeded":
            return
        payload = {
            "version": 1,
            "job": job,
            "grid": self._serialize_grid(grid),
            "height_grid": self._serialize_grid(height_grid) if height_grid is not None else None,
            "height_warning": height_warning,
        }
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        temporary.replace(self.cache_path)

    def _fresh_cache_age(self, job: dict) -> float | None:
        forecast = job.get("forecast") or {}
        try:
            generated = utc_timestamp(forecast["generated_at_utc"])
            valid = utc_timestamp(forecast["valid_time_utc"])
        except (KeyError, TypeError, ValueError):
            return None
        now = self.clock()
        age = (now - generated).total_seconds()
        return age if 0 <= age < MAX_CACHE_AGE.total_seconds() and valid > now else None

    def regional_job(self, job_id: str):
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError(job_id)
            if self.jobs[job_id]["status"] != "succeeded" or job_id not in self.grids:
                raise WeatherUnavailable("区域天气尚未就绪，请等待预测完成。")
            grid, model = self.grids[job_id], self.models[job_id]
            generated = self.jobs[job_id]["forecast"]["generated_at_utc"]
        if grid.init + timedelta(hours=6) <= self.clock():
            raise WeatherUnavailable('本次起报时次已过期，请重新预测最新天气，不复用旧任务。')
        return grid, model, generated

    def sample(self, job_id: str, latitude: float, longitude: float, valid_time_utc: str | None = None) -> dict:
        grid, model, generated = self.regional_job(job_id)
        result = grid.sample(latitude, longitude, model, valid_time_utc)
        if utc_timestamp(result["valid_time_utc"]) <= self.clock():
            raise WeatherUnavailable("区域预报已过期，请重新预测。")
        result["generated_at_utc"] = generated
        return result

    def series(self, job_id: str, latitude: float, longitude: float, hours: int = 6) -> dict:
        if not isinstance(hours, int) or not 1 <= hours <= 6:
            raise WeatherUnavailable('地面趋势仅支持未来 1–6 小时。')
        grid, model, generated = self.regional_job(job_id)
        start = self.clock()
        samples = []
        for offset in range(1, hours + 1):
            when = start + timedelta(hours=offset)
            sample = grid.sample(latitude, longitude, model, iso_utc(when))
            samples.append({
                'offset_hours': offset,
                'interval_label': f'{offset - 1}–{offset}h',
                'valid_time_utc': sample['valid_time_utc'],
                'time_method': sample['time_method'],
                'values': {key: sample['values'][key] for key in ('t2m_K', 'u10_m_s', 'v10_m_s', 'msl_Pa', 'tp_6h_mm')},
                'derived': sample['derived'],
                'rain_interval': sample['rain_interval'],
            })
        max_wind = max(sample['derived']['wind_speed_10m_m_s'] for sample in samples)
        max_rain = max(sample['values']['tp_6h_mm'] for sample in samples)
        return {
            'point': {'latitude_deg': latitude, 'longitude_deg': longitude},
            'window_start_utc': iso_utc(start),
            'window_end_utc': iso_utc(start + timedelta(hours=hours)),
            'samples': samples,
            'rain': {
                'total_mm_6h': max_rain,
                'interval_start_utc': samples[0]['rain_interval']['start_utc'],
                'interval_end_utc': samples[0]['rain_interval']['end_utc'],
                'is_hourly_allocation': False,
                'note': '当前资料提供六小时累计降水，未提供 0–1 小时逐小时分配。',
            },
            'ground_risk': point_risk(max_wind, max_rain),
            'native_time_step_hours': 6,
            'time_method': 'hourly_samples_between_native_6h_forecasts',
            'generated_at_utc': generated,
        }

    def route(self, job_id: str, coordinates, departure_time_utc: str, speed_kmh: float, altitude_m: float, profile=None):
        grid, model, generated = self.regional_job(job_id)
        with self.lock:
            height_grid, warning = self.height_grids.get(job_id), self.height_warnings.get(job_id)
        result = align_route(grid, coordinates, departure_time_utc, speed_kmh, altitude_m, model, now=self.clock(),
                             height_grid=height_grid, height_warning=warning)
        result.update({'weather_risk': screen_route(result, coordinates, profile),
                       'job_id': job_id, 'generated_at_utc': generated})
        result['decision'] = build_decision(result)
        return result

    def start(self, *, force_refresh: bool = False) -> dict:
        with self.lock:
            if self.active:
                return {**copy.deepcopy(self.jobs[self.active]), "shared_in_progress": True}
            if not force_refresh and self.latest_successful_job_id:
                cached = self.jobs.get(self.latest_successful_job_id)
                age = self._fresh_cache_age(cached) if cached else None
                if cached is not None and age is not None:
                    return {**copy.deepcopy(cached), "cache_hit": True, "cache_age_seconds": age}
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
                self.height_grids.pop(old_id, None)
                self.height_warnings.pop(old_id, None)
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
            model = self.corrector(configured_path("WEATHER_CORRECTOR_PATH", default_corrector_path()))
            self.update(job_id, message="FuXi 正在生成未来 6/12/18/24 小时区域天气。")
            predicted = self.worker(input_path, False)
            init, valid = validate_metadata(predicted, self.clock())
            if any(predicted[name] != metadata[name] for name in (
                "init_time_utc", "valid_time_utc", "previous_time_utc", "lead_hours",
                "latitude_deg", "longitude_deg",
            )):
                raise WeatherUnavailable("推理期间起报场发生变化，请重新预测。")
            row = {**predicted, "init": init, "valid": valid}
            forecast = assemble_forecast(row, point_model(model, predicted["latitude_deg"], predicted["longitude_deg"]),
                                         historical=False)
            forecast["input_source"] = predicted.get("input_source", "Configured weather initial field")
            forecast['initialization_frames'] = predicted.get('initialization_frames', [])
            forecast["generated_at_utc"] = iso_utc(self.clock())
            if "region" in predicted:
                grid = RegionalWeather(predicted["region"], predicted["init_time_utc"], forecast["input_source"],
                                       input_frames=forecast['initialization_frames'])
                with self.lock:
                    self.grids[job_id] = grid
                    self.models[job_id] = model
                forecast["regional_coverage"] = grid.coverage()
                self.update(job_id, message='正在获取与区域时次匹配的高度风、地面阵风和能见度。')
                try:
                    height_grid = HeightWeather(self.height_provider(input_path), predicted['init_time_utc'])
                    with self.lock: self.height_grids[job_id] = height_grid
                    forecast['height_coverage'] = {**height_grid.coverage(), 'heights_m': list((10, 20, 30, 40, 50, 80, 100)), 'height_reference': 'AGL'}
                    forecast['height_status'] = 'available'
                except Exception as error:
                    # Keep genuine FuXi surface forecasts usable; never replace
                    # missing altitude winds with the 10 m surface prediction.
                    warning = str(error)
                    with self.lock: self.height_warnings[job_id] = warning
                    forecast.update({'height_status': 'unavailable', 'height_warning': warning})
                validate_metadata(predicted, self.clock())
                forecast['generated_at_utc'] = iso_utc(self.clock())
            self.update(job_id, status="succeeded", message="未来天气预报已生成。", forecast=forecast)
            try:
                self._persist_cache(job_id)
            except (OSError, TypeError, ValueError):
                # Keep the in-memory result usable if the local cache directory is unavailable.
                pass
        except (WeatherUnavailable, FileNotFoundError) as error:
            self.update(job_id, status="blocked", message=str(error), forecast=None)
        except Exception as error:
            self.update(job_id, status="failed", message=f"天气预测失败：{error}", forecast=None)


forecast_jobs = ForecastJobs(cache_path=DEFAULT_CACHE_PATH)

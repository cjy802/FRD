"""Point weather from archived FuXi forecasts with selective ridge correction.

ERA5 label columns are intentionally ignored during inference. This service
uses the all-data deployment refit and preserves FuXi V10 and MSL exactly.
Surface point indicators are demonstration rules, not route-flight limits.
"""

from __future__ import annotations

import csv
import math
import os
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PAIRS = PROJECT_ROOT / "app/point_data/fuxi_era5_pairs_6h.csv"
DEFAULT_MODEL = PROJECT_ROOT / "Web/mode/output_production/corrector_ridge.npz"
FIELDS = ("t2m_K", "u10_m_s", "v10_m_s", "msl_Pa", "tp_6h_mm")
CORRECTED_FIELDS = frozenset(("t2m_K", "u10_m_s", "tp_6h_mm"))
FEATURE_NAMES = tuple(f"fuxi_{field}" for field in FIELDS) + ("lead_hours", "doy_sin", "doy_cos")
TARGET_NAMES = tuple(f"residual_{field}" for field in FIELDS)
RULES = {
    "wind_attention_m_s": 8.0, "wind_elevated_m_s": 12.0,
    "rain_attention_6h_mm": 1.0, "rain_elevated_6h_mm": 5.0,
}


class WeatherUnavailable(ValueError):
    pass


def utc_timestamp(value: str) -> datetime:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise WeatherUnavailable("天气时刻必须包含时区，例如 2026-09-06T06:00:00Z。")
    return stamp.astimezone(timezone.utc)


def iso_utc(stamp: datetime) -> str:
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def load_corrector(model_path: Path) -> dict:
    with np.load(model_path, allow_pickle=False) as artifact:
        if tuple(artifact["feature_names"].tolist()) != FEATURE_NAMES:
            raise WeatherUnavailable("模型不是当前公共 8 特征模型。")
        if tuple(artifact["target_names"].tolist()) != TARGET_NAMES:
            raise WeatherUnavailable("模型输出变量与天气接口不匹配。")
        expected = {
            "coefficients": (9, 5), "feature_mean": (8,), "feature_scale": (8,),
            "target_mean": (5,), "target_scale": (5,),
        }
        model = {}
        for name, shape in expected.items():
            values = np.asarray(artifact[name], dtype=np.float64)
            if values.shape != shape or not np.isfinite(values).all():
                raise WeatherUnavailable(f"模型参数 {name} 无效。")
            model[name] = values.copy()
        if np.any(model["feature_scale"] <= 0) or np.any(model["target_scale"] <= 0):
            raise WeatherUnavailable("模型标准化尺度无效。")
        return model


def assemble_forecast(row: dict, model: dict | None, model_warning: str | None = None, historical: bool = True) -> dict:
    raw = dict(row["raw"])
    if not all(math.isfinite(raw[field]) for field in FIELDS):
        raise WeatherUnavailable("FuXi 点位预报包含非有限数值。")
    final = dict(raw)
    sources = {field: "fuxi" for field in FIELDS}
    if model is not None:
        stamp = row["valid"]
        fraction = stamp.timetuple().tm_yday - 1.0 + stamp.hour / 24.0
        angle = 2 * np.pi * fraction / 365.25
        features = np.asarray([*[raw[field] for field in FIELDS], row["lead_hours"], np.sin(angle), np.cos(angle)])
        design = np.r_[1.0, (features - model["feature_mean"]) / model["feature_scale"]]
        residual = design @ model["coefficients"] * model["target_scale"] + model["target_mean"]
        if not np.isfinite(residual).all():
            raise WeatherUnavailable("订正模型输出了非有限数值。")
        for index, field in enumerate(FIELDS):
            if field in CORRECTED_FIELDS:
                final[field] = raw[field] + float(residual[index])
                sources[field] = "ridge"
        final["tp_6h_mm"] = max(0.0, final["tp_6h_mm"])
    speed = math.hypot(final["u10_m_s"], final["v10_m_s"])
    direction = (math.degrees(math.atan2(-final["u10_m_s"], -final["v10_m_s"])) + 360) % 360 if speed > 1e-12 else None
    risk = point_risk(speed, final["tp_6h_mm"])
    if not historical:
        risk["scope"] = "forecast_point_surface"
        risk["message"] = "仅评估该预报点位的 10 米风和 6 小时降水，阈值为演示规则，不是飞行限值。"
    return {
        "init_time_utc": iso_utc(row["init"]), "valid_time_utc": iso_utc(row["valid"]),
        "lead_hours": row["lead_hours"],
        "point": {"latitude_deg": row["latitude_deg"], "longitude_deg": row["longitude_deg"]},
        "raw": raw, "values": final, "sources": sources,
        "derived": {
            "t2m_C": final["t2m_K"] - 273.15, "msl_hPa": final["msl_Pa"] / 100,
            "wind_speed_10m_m_s": speed, "wind_direction_from_deg": direction,
        },
        "historical": historical, "ready_for_route_risk": False,
        "model_warning": model_warning, "point_risk": risk,
    }


class PointWeatherStore:
    def __init__(self, data_path: Path, model_path: Path, archive_year: int = 2026):
        self.data_path = data_path
        self.model_path = model_path
        self.archive_year = archive_year
        self.rows = self._load_rows()
        self.model = None
        self.model_error = None
        try:
            self.model = self._load_model()
        except (OSError, ValueError, KeyError) as error:
            self.model_error = f"订正模型不可用，当前全部使用 FuXi 原值：{error}"

    def _load_rows(self) -> list[dict]:
        rows = []
        seen = set()
        with self.data_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            required = {"init_time_utc", "valid_time_utc", "lead_hours", "latitude_deg", "longitude_deg"}
            required.update(f"fuxi_{field}" for field in FIELDS)
            if not required.issubset(reader.fieldnames or ()):
                raise WeatherUnavailable("天气 CSV 缺少 FuXi 数值或预报时间、点位列。")
            for record in reader:
                init = utc_timestamp(record["init_time_utc"])
                valid = utc_timestamp(record["valid_time_utc"])
                if init.year != self.archive_year:
                    continue
                lead = float(record["lead_hours"])
                latitude = float(record["latitude_deg"])
                longitude = float(record["longitude_deg"])
                raw = {field: float(record[f"fuxi_{field}"]) for field in FIELDS}
                if not all(math.isfinite(v) for v in (lead, latitude, longitude, *raw.values())):
                    raise WeatherUnavailable("天气 CSV 包含非有限数值。")
                if lead <= 0 or lead % 6 or (valid - init).total_seconds() != lead * 3600:
                    raise WeatherUnavailable("起报时间、提前量和有效时间不一致。")
                if not (-90 <= latitude <= 90 and -180 <= longitude <= 360):
                    raise WeatherUnavailable("天气点位经纬度无效。")
                key = (init, int(lead), latitude, longitude)
                if key in seen:
                    raise WeatherUnavailable("天气 CSV 包含重复预报。")
                seen.add(key)
                rows.append({
                    "init": init, "valid": valid, "lead_hours": int(lead),
                    "latitude_deg": latitude, "longitude_deg": longitude, "raw": raw,
                })
        if not rows:
            raise WeatherUnavailable(f"天气 CSV 中没有 {self.archive_year} 年的预报。")
        points = {(row["latitude_deg"], row["longitude_deg"]) for row in rows}
        if len(points) != 1:
            raise WeatherUnavailable("当前接口仅支持单点天气 CSV，不能自动混用多个点位。")
        return sorted(rows, key=lambda row: (row["valid"], row["init"], row["lead_hours"]), reverse=True)

    def _load_model(self) -> dict:
        return load_corrector(self.model_path)

    def status(self) -> dict:
        first = self.rows[0]
        policy = {
            field: "ridge" if self.model is not None and field in CORRECTED_FIELDS else "fuxi"
            for field in FIELDS
        }
        return {
            "state": "corrected_archive" if self.model is not None else "raw_archive",
            "source": self.data_path.name,
            "model_source": self.model_path.name if self.model is not None else None,
            "ready_for_route_risk": False,
            "message": self.model_error or "已接入历史单点预报：温度、U 风、降水订正，V 风和气压使用 FuXi 原值。",
            "historical": True, "archive_year": self.archive_year,
            "rows": len(self.rows), "field_policy": policy,
            "point": {"latitude_deg": first["latitude_deg"], "longitude_deg": first["longitude_deg"]},
            "valid_start_utc": iso_utc(self.rows[-1]["valid"]),
            "valid_end_utc": iso_utc(first["valid"]),
            "options": [{
                "init_time_utc": iso_utc(row["init"]), "valid_time_utc": iso_utc(row["valid"]),
                "lead_hours": row["lead_hours"],
            } for row in self.rows],
        }

    def forecast(self, init_time_utc: str | None = None, lead_hours: int | None = None) -> dict:
        init = utc_timestamp(init_time_utc) if init_time_utc else None
        candidates = [row for row in self.rows if (
            (init is None or row["init"] == init)
            and (lead_hours is None or row["lead_hours"] == lead_hours)
        )]
        if not candidates:
            raise WeatherUnavailable("所选起报时间和提前量没有天气记录；未替换为其他日期。")
        return assemble_forecast(candidates[0], self.model, self.model_error)


def point_risk(wind_speed: float, rain_6h: float) -> dict:
    triggers = []
    elevated = False
    if wind_speed >= RULES["wind_attention_m_s"]:
        high = wind_speed >= RULES["wind_elevated_m_s"]
        elevated |= high
        triggers.append({
            "field": "wind_speed_10m_m_s", "label": "地面风偏强",
            "value": wind_speed, "unit": "m/s",
            "threshold": RULES["wind_elevated_m_s"] if high else RULES["wind_attention_m_s"],
        })
    if rain_6h >= RULES["rain_attention_6h_mm"]:
        high = rain_6h >= RULES["rain_elevated_6h_mm"]
        elevated |= high
        triggers.append({
            "field": "tp_6h_mm", "label": "有降水提示", "value": rain_6h, "unit": "mm/6h",
            "threshold": RULES["rain_elevated_6h_mm"] if high else RULES["rain_attention_6h_mm"],
        })
    level = "elevated" if elevated else "attention" if triggers else "no_trigger"
    return {
        "level": level,
        "label": {"elevated": "单点较强提示", "attention": "单点关注", "no_trigger": "未触发单点阈值"}[level],
        "triggers": triggers, "rules": RULES,
        "scope": "historical_point_surface", "ready_for_route_risk": False,
        "message": "仅评估所选历史点位的 10 米风和 6 小时降水，阈值为演示规则，不是飞行限值。",
        "route_reason": "航线气象仍待评估：缺少沿线、飞行高度及任务时刻匹配的气象数据。",
    }


def configured_path(name: str, default: Path) -> Path:
    value = os.getenv(name, "").strip()
    path = Path(value) if value else default
    return path if path.is_absolute() else PROJECT_ROOT / path


@lru_cache(maxsize=2)
def _cached_store(data_path: str, model_path: str, year: int, data_mtime: int, model_mtime: int) -> PointWeatherStore:
    return PointWeatherStore(Path(data_path), Path(model_path), year)


def weather_store() -> PointWeatherStore:
    data_path = configured_path("WEATHER_PAIRS_PATH", DEFAULT_PAIRS)
    model_path = configured_path("WEATHER_CORRECTOR_PATH", DEFAULT_MODEL)
    year = int(os.getenv("WEATHER_ARCHIVE_YEAR", "2026"))
    try:
        data_mtime = data_path.stat().st_mtime_ns
    except OSError as error:
        raise WeatherUnavailable("没有找到天气预报 CSV，请检查 WEATHER_PAIRS_PATH。") from error
    model_mtime = model_path.stat().st_mtime_ns if model_path.is_file() else -1
    return _cached_store(str(data_path), str(model_path), year, data_mtime, model_mtime)

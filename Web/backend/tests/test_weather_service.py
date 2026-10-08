import csv
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient

from main import app
from weather_service import (
    DEFAULT_MODEL, DEFAULT_PAIRS, FEATURE_NAMES, FIELDS, TARGET_NAMES,
    PROJECT_ROOT, PointWeatherStore, WeatherUnavailable, point_risk,
)


class WeatherServiceTests(unittest.TestCase):
    def setUp(self):
        output = PROJECT_ROOT / "output"
        output.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="weather_test_", dir=output)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "forecasts.csv"
        self.model = self.root / "model.npz"
        columns = ["init_time_utc", "valid_time_utc", "lead_hours", "latitude_deg", "longitude_deg"]
        columns += [f"fuxi_{field}" for field in FIELDS]
        columns += [f"era5_{field}" for field in FIELDS]
        self.record = {
            "init_time_utc": "2026-01-02T06:00:00Z", "valid_time_utc": "2026-01-02T12:00:00Z",
            "lead_hours": 6, "latitude_deg": 31.5, "longitude_deg": 119.5,
            "fuxi_t2m_K": 281.0, "fuxi_u10_m_s": 4.0, "fuxi_v10_m_s": 3.0,
            "fuxi_msl_Pa": 101000.0, "fuxi_tp_6h_mm": 2.0,
            **{f"era5_{field}": 999999.0 for field in FIELDS},
        }
        self.columns = columns
        self.write_data()
        self.write_model()

    def write_data(self):
        with self.data.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.columns)
            writer.writeheader()
            writer.writerow(self.record)

    def write_model(self, residual=(1, 2, 99, 100000, -4)):
        coefficients = np.zeros((9, 5))
        coefficients[0] = residual
        np.savez(
            self.model, coefficients=coefficients, feature_mean=np.zeros(8),
            feature_scale=np.ones(8), target_mean=np.zeros(5), target_scale=np.ones(5),
            feature_names=np.asarray(FEATURE_NAMES), target_names=np.asarray(TARGET_NAMES),
        )

    def store(self):
        return PointWeatherStore(self.data, self.model)

    def test_only_requested_variables_are_corrected_and_rain_is_nonnegative(self):
        forecast = self.store().forecast()
        values = forecast["values"]
        self.assertEqual(values, {
            "t2m_K": 282, "u10_m_s": 6, "v10_m_s": 3,
            "msl_Pa": 101000, "tp_6h_mm": 0,
        })
        self.assertEqual(forecast["sources"]["v10_m_s"], "fuxi")
        self.assertEqual(forecast["sources"]["msl_Pa"], "fuxi")
        self.assertEqual(forecast["sources"]["tp_6h_mm"], "ridge")
        self.assertAlmostEqual(forecast["derived"]["wind_speed_10m_m_s"], math.hypot(6, 3))
        self.assertEqual(forecast["point_risk"]["triggers"], [])

    def test_risk_uses_corrected_u_and_raw_v(self):
        self.record["fuxi_u10_m_s"] = 7
        self.record["fuxi_v10_m_s"] = 0
        self.write_data()
        self.write_model(residual=(0, 5, 99, 100000, -4))
        forecast = self.store().forecast()
        self.assertEqual(forecast["derived"]["wind_speed_10m_m_s"], 12)
        self.assertEqual(forecast["derived"]["wind_direction_from_deg"], 270)
        self.assertEqual(forecast["point_risk"]["level"], "elevated")
        self.assertEqual(forecast["point_risk"]["triggers"][0]["value"], 12)

    def test_era5_labels_do_not_affect_inference(self):
        original = self.store().forecast()["values"]
        for field in FIELDS:
            self.record[f"era5_{field}"] = -123456
        self.write_data()
        self.assertEqual(original, self.store().forecast()["values"])

    def test_missing_model_is_explicit_raw_fallback(self):
        store = PointWeatherStore(self.data, self.root / "missing.npz")
        forecast = store.forecast()
        self.assertEqual(forecast["values"], forecast["raw"])
        self.assertTrue(all(source == "fuxi" for source in forecast["sources"].values()))
        self.assertIsNotNone(forecast["model_warning"])
        self.assertEqual(store.status()["state"], "raw_archive")

    def test_unknown_time_is_rejected_without_latest_fallback(self):
        with self.assertRaisesRegex(WeatherUnavailable, "没有天气记录"):
            self.store().forecast("2026-01-03T06:00:00Z", 6)

    def test_invalid_time_alignment_is_rejected(self):
        self.record["valid_time_utc"] = "2026-01-02T13:00:00Z"
        self.write_data()
        with self.assertRaisesRegex(WeatherUnavailable, "不一致"):
            self.store()

    def test_no_point_rule_can_mark_route_safe(self):
        for wind, rain in [(0, 0), (8, 1), (12, 5)]:
            self.assertFalse(point_risk(wind, rain)["ready_for_route_risk"])

    def test_weather_endpoints_share_policy_and_time(self):
        store = self.store()
        with patch("main.weather_store", return_value=store):
            client = TestClient(app)
            status = client.get("/api/weather/status").json()
            forecast = client.get("/api/weather/forecast").json()
            risk = client.get("/api/weather/risk").json()
            missing = client.get("/api/weather/forecast", params={"init_time_utc": "2026-02-01T06:00:00Z"})
        self.assertEqual(status["state"], "corrected_archive")
        self.assertEqual(forecast["valid_time_utc"], risk["valid_time_utc"])
        self.assertEqual(forecast["sources"], risk["sources"])
        self.assertEqual(forecast["derived"], risk["derived"])
        self.assertFalse(risk["ready_for_route_risk"])
        self.assertEqual(missing.status_code, 422)

    @unittest.skipUnless(
        DEFAULT_PAIRS.is_file() and DEFAULT_MODEL.is_file()
        and (DEFAULT_MODEL.parent / "test_predictions.csv").is_file(),
        "Local weather data/model artifacts are not part of the repository",
    )
    def test_saved_model_matches_training_inference_for_real_data(self):
        store = PointWeatherStore(DEFAULT_PAIRS, DEFAULT_MODEL)
        forecast = store.forecast()
        prediction_csv = DEFAULT_MODEL.parent / "test_predictions.csv"
        with prediction_csv.open("r", encoding="utf-8-sig", newline="") as handle:
            row = next(record for record in csv.DictReader(handle) if record["init_time_utc"] == forecast["init_time_utc"])
        for field in ("t2m_K", "u10_m_s", "tp_6h_mm"):
            self.assertAlmostEqual(forecast["values"][field], float(row[f"ridge_{field}"]), places=8)
        for field in ("v10_m_s", "msl_Pa"):
            self.assertEqual(forecast["values"][field], forecast["raw"][field])


if __name__ == "__main__":
    unittest.main()

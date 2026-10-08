import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
from fastapi.testclient import TestClient

from main import app
from live_weather import ForecastJobs, validate_metadata
from weather_service import WeatherUnavailable


NOW = datetime(2026, 10, 8, 1, tzinfo=timezone.utc)
METADATA = {
    "init_time_utc": "2026-10-08T00:00:00Z", "previous_time_utc": "2026-10-07T18:00:00Z",
    "valid_time_utc": "2026-10-08T06:00:00Z", "lead_hours": 6,
    "latitude_deg": 31.5, "longitude_deg": 119.5,
}
RAW = {"t2m_K": 280., "u10_m_s": 4., "v10_m_s": 3., "msl_Pa": 101000., "tp_6h_mm": 2.}


def model():
    coefficients = np.zeros((9, 5))
    coefficients[0] = [1, 2, 99, 100000, -4]
    return {
        "coefficients": coefficients, "feature_mean": np.zeros(8), "feature_scale": np.ones(8),
        "target_mean": np.zeros(5), "target_scale": np.ones(5),
    }


def wait_for_job(manager, job):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        result = manager.get(job["job_id"])
        if result["status"] in ("succeeded", "failed", "blocked"):
            return result
        time.sleep(0.005)
    raise AssertionError("Mock prediction did not complete")


class LiveWeatherTests(unittest.TestCase):
    def manager(self, worker=None, clock=None, corrector=None):
        manager = ForecastJobs(
            worker=worker or (lambda path, inspect: METADATA if inspect else {**METADATA, "raw": RAW}),
            clock=clock or (lambda: NOW), corrector=corrector or (lambda path: model()),
        )
        self.addCleanup(manager.executor.shutdown, wait=True)
        return manager

    def test_missing_input_blocks_without_using_historical_csv(self):
        worker = Mock()
        manager = self.manager(worker=worker)
        with patch("live_weather.Path.is_file", return_value=False):
            result = wait_for_job(manager, manager.start())
        self.assertEqual(result["status"], "blocked")
        self.assertIsNone(result["forecast"])
        self.assertIn("最新全球起报场", result["message"])
        worker.assert_not_called()

    def test_current_input_runs_prediction_and_only_corrects_selected_fields(self):
        worker = Mock(side_effect=lambda path, inspect: METADATA if inspect else {**METADATA, "raw": RAW})
        manager = self.manager(worker=worker)
        with patch("live_weather.Path.is_file", return_value=True):
            result = wait_for_job(manager, manager.start())
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(worker.call_count, 2)
        forecast = result["forecast"]
        self.assertFalse(forecast["historical"])
        self.assertEqual(forecast["values"], {
            "t2m_K": 281., "u10_m_s": 6., "v10_m_s": 3., "msl_Pa": 101000., "tp_6h_mm": 0.,
        })
        self.assertEqual(forecast["point_risk"]["scope"], "forecast_point_surface")

    def test_stale_input_is_rejected_before_gpu_prediction(self):
        metadata = {**METADATA, "init_time_utc": "2026-01-15T06:00:00Z",
                    "previous_time_utc": "2026-01-15T00:00:00Z", "valid_time_utc": "2026-01-15T12:00:00Z"}
        worker = Mock(return_value=metadata)
        manager = self.manager(worker=worker)
        with patch("live_weather.Path.is_file", return_value=True):
            result = wait_for_job(manager, manager.start())
        self.assertEqual(result["status"], "blocked")
        self.assertIn("已过期", result["message"])
        self.assertEqual(worker.call_count, 1)

    def test_new_entry_does_not_reuse_completed_result(self):
        manager = self.manager()
        with patch("live_weather.Path.is_file", return_value=True):
            first = wait_for_job(manager, manager.start())
            second = wait_for_job(manager, manager.start())
        self.assertNotEqual(first["job_id"], second["job_id"])
        self.assertEqual(second["status"], "succeeded")

    def test_concurrent_entries_share_only_in_progress_job(self):
        running, release = threading.Event(), threading.Event()
        def worker(path, inspect):
            if inspect:
                return METADATA
            running.set()
            if not release.wait(1):
                raise AssertionError("GPU mock was not released")
            return {**METADATA, "raw": RAW}
        manager = self.manager(worker=worker)
        with patch("live_weather.Path.is_file", return_value=True):
            first = manager.start()
            self.assertTrue(running.wait(1))
            second = manager.start()
            self.assertEqual(first["job_id"], second["job_id"])
            self.assertTrue(second["shared_in_progress"])
            release.set()
            self.assertEqual(wait_for_job(manager, first)["status"], "succeeded")

    def test_expiry_during_prediction_does_not_publish_old_result(self):
        times = iter([NOW, NOW, NOW + timedelta(hours=6)])
        manager = self.manager(clock=lambda: next(times))
        with patch("live_weather.Path.is_file", return_value=True):
            result = wait_for_job(manager, manager.start())
        self.assertEqual(result["status"], "blocked")
        self.assertIsNone(result["forecast"])

    def test_invalid_future_origin_and_wrong_point_are_rejected(self):
        for metadata in [
            {**METADATA, "init_time_utc": "2026-10-08T06:00:00Z",
             "previous_time_utc": "2026-10-08T00:00:00Z", "valid_time_utc": "2026-10-08T12:00:00Z"},
            {**METADATA, "latitude_deg": 31.75},
        ]:
            with self.assertRaises(WeatherUnavailable):
                validate_metadata(metadata, NOW)

    def test_job_routes_return_status_and_unknown_job_404(self):
        manager = Mock()
        manager.start.return_value = {"job_id": "test", "status": "queued", "message": "queued", "forecast": None}
        manager.get.return_value = manager.start.return_value
        with patch("main.forecast_jobs", manager):
            client = TestClient(app)
            self.assertEqual(client.post("/api/weather/predict").json()["job_id"], "test")
            self.assertEqual(client.get("/api/weather/predictions/test").json()["status"], "queued")
            manager.get.side_effect = KeyError("unknown")
            self.assertEqual(client.get("/api/weather/predictions/unknown").status_code, 404)


if __name__ == "__main__":
    unittest.main()

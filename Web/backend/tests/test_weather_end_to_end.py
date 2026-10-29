import unittest
from datetime import timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient

from main import app
from live_weather import ForecastJobs
from test_height_weather import height_payload
from test_live_weather import METADATA, NOW, RAW, model, wait_for_job
from test_route_weather import temporal_payload
from weather_service import WeatherUnavailable
from auth_test_support import login_admin


class EndToEndWeatherTests(unittest.TestCase):
    def manager(self, clock=None, provider=None):
        manager = ForecastJobs(worker=lambda path, inspect: METADATA if inspect else {**METADATA, 'raw': RAW, 'region': temporal_payload()},
                               clock=clock or (lambda: NOW), corrector=lambda path: model(), preparer=lambda path: path,
                               height_provider=provider or (lambda path: height_payload()))
        self.addCleanup(manager.executor.shutdown, wait=True)
        return manager

    def ready_job(self, manager):
        with patch('live_weather.Path.is_file', return_value=True): return wait_for_job(manager, manager.start())

    def test_current_job_route_and_limits_share_data_without_new_prediction(self):
        manager = self.manager()
        result = self.ready_job(manager)
        payload = {'coordinates': [[119.51, 31.51], [119.6, 31.6]], 'departure_time_utc': '2026-10-08T03:00:00Z', 'altitude_m': 100, 'speed_kmh': 45}
        with patch('main.forecast_jobs', manager), patch.object(manager, 'worker', wraps=manager.worker) as worker:
            client = TestClient(app)
            login_admin(client)
            assessment = client.post(f"/api/weather/predictions/{result['job_id']}/route", json=payload).json()
            self.assertTrue(assessment['weather_risk']['coverage_complete'])
            self.assertIsNone(assessment['decision']['may_fly'])
            self.assertEqual(worker.call_count, 0)

    def test_old_origin_rejected_even_if_requested_forecast_time_is_in_future(self):
        manager = self.manager()
        result = self.ready_job(manager)
        manager.clock = lambda: NOW + timedelta(hours=6)
        with self.assertRaisesRegex(WeatherUnavailable, '过期'):
            manager.route(result['job_id'], [[119.51, 31.51], [119.6, 31.6]], '2026-10-08T12:00:00Z', 45, 100)
        with self.assertRaisesRegex(WeatherUnavailable, '过期'):
            manager.sample(result['job_id'], 31.5, 119.5, '2026-10-08T12:00:00Z')

    def test_origin_expiry_during_auxiliary_fetch_does_not_publish_stale_success(self):
        current = {'now': NOW}
        def provider(path):
            current['now'] = NOW + timedelta(hours=6)
            return height_payload()
        manager = self.manager(clock=lambda: current['now'], provider=provider)
        result = self.ready_job(manager)
        self.assertEqual(result['status'], 'blocked')
        self.assertIsNone(result['forecast'])

    def test_invalid_time_height_and_unknown_jobs_return_explicit_api_results(self):
        manager = self.manager()
        result = self.ready_job(manager)
        payload = {'coordinates': [[119.51, 31.51], [119.6, 31.6]], 'departure_time_utc': '2026-10-08T03:00:00Z', 'altitude_m': 100}
        with patch('main.forecast_jobs', manager):
            client = TestClient(app)
            login_admin(client)
            path = f"/api/weather/predictions/{result['job_id']}/route"
            self.assertEqual(client.post(path, json={**payload, 'departure_time_utc': '2026-10-08T00:00:00Z'}).status_code, 422)
            unsupported = client.post(path, json={**payload, 'altitude_m': 120}).json()
            self.assertEqual(unsupported['weather_risk']['level'], 'incomplete')
            self.assertIsNone(unsupported['decision']['may_fly'])


if __name__ == '__main__': unittest.main()

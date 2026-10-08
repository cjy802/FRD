import unittest
from datetime import timedelta
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from requests import Timeout

from main import app, road_candidates_cached
from live_weather import ForecastJobs
from online_router import _fetch_route, plan_online_alternatives, OnlineServiceUnavailable
from test_height_weather import height_payload
from test_live_weather import METADATA, NOW, RAW, model, wait_for_job
from test_route_weather import temporal_payload
from weather_service import WeatherUnavailable


class EndToEndWeatherTests(unittest.TestCase):
    def manager(self, clock=None, provider=None):
        manager = ForecastJobs(worker=lambda path, inspect: METADATA if inspect else {**METADATA, 'raw': RAW, 'region': temporal_payload()},
                               clock=clock or (lambda: NOW), corrector=lambda path: model(), preparer=lambda path: path,
                               height_provider=provider or (lambda path: height_payload()))
        self.addCleanup(manager.executor.shutdown, wait=True)
        return manager

    def ready_job(self, manager):
        with patch('live_weather.Path.is_file', return_value=True): return wait_for_job(manager, manager.start())

    def test_current_job_route_windows_and_limits_share_data_without_new_prediction(self):
        manager = self.manager()
        result = self.ready_job(manager)
        payload = {'coordinates': [[119.51, 31.51], [119.6, 31.6]], 'departure_time_utc': '2026-10-08T03:00:00Z', 'altitude_m': 100, 'speed_kmh': 45}
        with patch('main.forecast_jobs', manager), patch.object(manager, 'worker', wraps=manager.worker) as worker:
            client = TestClient(app)
            assessment = client.post(f"/api/weather/predictions/{result['job_id']}/route", json=payload).json()
            windows = client.post(f"/api/weather/predictions/{result['job_id']}/windows", json=payload).json()
            self.assertTrue(assessment['weather_risk']['coverage_complete'])
            self.assertIsNone(assessment['decision']['may_fly'])
            self.assertGreater(windows['covered_count'], 0)
            self.assertEqual(windows['generated_at_utc'], assessment['generated_at_utc'])
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
            path = f"/api/weather/predictions/{result['job_id']}/route"
            self.assertEqual(client.post(path, json={**payload, 'departure_time_utc': '2026-10-08T00:00:00Z'}).status_code, 422)
            unsupported = client.post(path, json={**payload, 'altitude_m': 120}).json()
            self.assertEqual(unsupported['weather_risk']['level'], 'incomplete')
            self.assertIsNone(unsupported['decision']['may_fly'])
            self.assertEqual(client.post('/api/weather/predictions/missing/windows', json=payload).status_code, 404)

    def test_online_alternatives_are_real_service_geometries_and_failure_not_local_fallback(self):
        payload = {'code': 'Ok', 'waypoints': [{'distance': 0}, {'distance': 0}],
                   'routes': [{'distance': 300, 'geometry': {'coordinates': [[119.9515, 31.6792], [119.9495, 31.6804]]}},
                              {'distance': 350, 'geometry': {'coordinates': [[119.9515, 31.6792], [119.95, 31.6805], [119.9495, 31.6804]]}}]}
        with patch('online_router._fetch_route', return_value=payload) as request:
            candidates = plan_online_alternatives(31.6792, 119.9515, 31.6804, 119.9495)
            self.assertEqual(len(candidates), 2)
            self.assertTrue(request.call_args.args[-1])
            self.assertTrue(all(candidate['building_clearance_m'] is None for candidate in candidates))
        manager = Mock()
        manager.regional_job.return_value = (Mock(), None, 'now')
        road_candidates_cached.cache_clear(); _fetch_route.cache_clear()
        request_body = {'weather_job_id': 'test', 'start': {'latitude': 31.6792, 'longitude': 119.9515},
                        'end': {'latitude': 31.6804, 'longitude': 119.9495}, 'road_source': 'online',
                        'departure_time_utc': '2026-10-08T03:00:00Z'}
        with patch('main.forecast_jobs', manager), patch('online_router.requests.get', side_effect=Timeout), patch('main.local_road_store') as local:
            self.assertEqual(TestClient(app).post('/api/routes/compare-weather', json=request_body).status_code, 503)
            local.assert_not_called()
        road_candidates_cached.cache_clear(); _fetch_route.cache_clear()


if __name__ == '__main__': unittest.main()

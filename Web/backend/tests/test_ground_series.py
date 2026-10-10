import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from main import app
from live_weather import ForecastJobs
from test_live_weather import METADATA, NOW, RAW, model, wait_for_job
from test_route_weather import temporal_payload
from weather_service import WeatherUnavailable


class GroundSeriesTests(unittest.TestCase):
    def manager(self):
        manager = ForecastJobs(
            worker=lambda path, inspect: METADATA if inspect else {**METADATA, 'raw': RAW, 'region': temporal_payload()},
            clock=lambda: NOW, corrector=lambda path: model(), preparer=lambda path: path,
            height_provider=lambda path: (_ for _ in ()).throw(WeatherUnavailable('No height in series test')),
        )
        self.addCleanup(manager.executor.shutdown, wait=True)
        return manager

    def ready(self, manager):
        with patch('live_weather.Path.is_file', return_value=True): return wait_for_job(manager, manager.start())

    def test_returns_hourly_ground_series_and_keeps_rain_as_six_hour_total(self):
        manager = self.manager()
        job = self.ready(manager)
        result = manager.series(job['job_id'], 31.625, 119.625, 6)
        self.assertEqual([item['interval_label'] for item in result['samples']], ['0–1h', '1–2h', '2–3h', '3–4h', '4–5h', '5–6h'])
        self.assertEqual([item['time_method'] for item in result['samples']], [
            'linear_time_estimate', 'linear_time_estimate', 'linear_time_estimate', 'linear_time_estimate',
            'native_time', 'linear_time_estimate',
        ])
        self.assertFalse(result['rain']['is_hourly_allocation'])
        self.assertIn('六小时累计降水', result['rain']['note'])
        self.assertEqual(result['point'], {'latitude_deg': 31.625, 'longitude_deg': 119.625})

    def test_invalid_horizon_outside_grid_and_api_status_codes(self):
        manager = self.manager()
        job = self.ready(manager)
        with self.assertRaises(WeatherUnavailable): manager.series(job['job_id'], 31.625, 119.625, 7)
        with self.assertRaises(WeatherUnavailable): manager.series(job['job_id'], 20, 119.625, 1)
        client = TestClient(app)
        with patch('main.forecast_jobs.series', return_value={'samples': []}) as series:
            response = client.get('/api/weather/predictions/test/series?latitude=31.5&longitude=119.5&hours=6')
            self.assertEqual(response.status_code, 200)
            series.assert_called_once_with('test', 31.5, 119.5, 6)
        with patch('main.forecast_jobs.series', side_effect=WeatherUnavailable('范围外')):
            self.assertEqual(client.get('/api/weather/predictions/test/series?latitude=20&longitude=119&hours=6').status_code, 422)
        with patch('main.forecast_jobs.series', side_effect=KeyError('test')):
            self.assertEqual(client.get('/api/weather/predictions/test/series?latitude=31.5&longitude=119.5&hours=6').status_code, 404)


if __name__ == '__main__': unittest.main()

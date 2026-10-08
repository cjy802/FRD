import copy
import unittest
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient

from main import app
from regional_weather import RegionalWeather
from weather_service import WeatherUnavailable
from test_live_weather import METADATA, NOW, RAW, model, wait_for_job
from live_weather import ForecastJobs


def region_payload():
    values = np.asarray([[[RAW[field], RAW[field] + 1], [RAW[field] + 2, RAW[field] + 3]] for field in RAW])
    return {'latitudes': [31.75, 31.5], 'longitudes': [119.5, 119.75],
            'channels': ['T2M', 'U10', 'V10', 'MSL', 'TP'],
            'snapshots': [{'valid_time_utc': METADATA['valid_time_utc'], 'lead_hours': 6, 'values': values.tolist()}]}


class RegionalWeatherTests(unittest.TestCase):
    def grid(self, payload=None):
        return RegionalWeather(payload or region_payload(), METADATA['init_time_utc'], 'NOAA GFS')

    def test_native_cell_bilinear_interpolation_and_edges(self):
        grid = self.grid()
        self.assertEqual(grid.sample(31.625, 119.625)['raw']['t2m_K'], RAW['t2m_K'] + 1.5)
        self.assertEqual(grid.sample(31.5, 119.75)['raw']['msl_Pa'], RAW['msl_Pa'] + 3)
        self.assertEqual(grid.coverage()['native_resolution_deg'], .25)

    def test_only_training_point_uses_corrector_v_and_pressure_unchanged(self):
        grid = self.grid()
        point = grid.sample(31.5, 119.5, model())
        self.assertTrue(point['corrector_applicable'])
        self.assertEqual(point['values']['t2m_K'], point['raw']['t2m_K'] + 1)
        for field in ('v10_m_s', 'msl_Pa'):
            self.assertEqual(point['raw'][field], point['values'][field])
        away = grid.sample(31.625, 119.625, model())
        self.assertFalse(away['corrector_applicable'])
        self.assertEqual(away['values'], away['raw'])
        self.assertIn('未在该位置验证', away['model_warning'])

    def test_outside_or_nonfinite_coordinate_is_not_silently_snapped(self):
        for lat, lon in [(31, 119.5), (31.5, 120), (float('nan'), 119.5)]:
            with self.assertRaises(WeatherUnavailable):
                self.grid().sample(lat, lon)

    def test_invalid_grid_time_and_nonfinite_data_are_rejected(self):
        for kind in ('spacing', 'values', 'time', 'duplicate'):
            payload = copy.deepcopy(region_payload())
            if kind == 'spacing': payload['longitudes'][1] = 120
            if kind == 'values': payload['snapshots'][0]['values'][0][0][0] = float('nan')
            if kind == 'time': payload['snapshots'][0]['lead_hours'] = 12
            if kind == 'duplicate': payload['snapshots'].append(payload['snapshots'][0])
            with self.assertRaises(WeatherUnavailable): self.grid(payload)

    def test_sample_api_status_codes(self):
        client = TestClient(app)
        with patch('main.forecast_jobs.sample', return_value={'point': {'latitude_deg': 31.5}}) as sample:
            response = client.get('/api/weather/predictions/test/sample?latitude=31.5&longitude=119.5')
            self.assertEqual(response.status_code, 200)
            sample.assert_called_once_with('test', 31.5, 119.5)
        with patch('main.forecast_jobs.sample', side_effect=WeatherUnavailable('范围外')):
            self.assertEqual(client.get('/api/weather/predictions/test/sample?latitude=31&longitude=119').status_code, 422)
        with patch('main.forecast_jobs.sample', side_effect=KeyError('test')):
            self.assertEqual(client.get('/api/weather/predictions/test/sample?latitude=31&longitude=119').status_code, 404)

    def test_finished_job_keeps_region_for_queries_without_new_inference(self):
        calls = []
        def worker(path, inspect):
            calls.append(inspect)
            return METADATA if inspect else {**METADATA, 'raw': RAW, 'region': region_payload()}
        manager = ForecastJobs(worker=worker, clock=lambda: NOW, corrector=lambda path: model(), preparer=lambda path: path,
                               height_provider=lambda path: (_ for _ in ()).throw(WeatherUnavailable('Mock: no height data')))
        self.addCleanup(manager.executor.shutdown, wait=True)
        with patch('live_weather.Path.is_file', return_value=True):
            result = wait_for_job(manager, manager.start())
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(manager.sample(result['job_id'], 31.625, 119.625)['raw']['t2m_K'], RAW['t2m_K'] + 1.5)
        manager.sample(result['job_id'], 31.5, 119.5)
        self.assertEqual(calls, [True, False])


if __name__ == '__main__':
    unittest.main()

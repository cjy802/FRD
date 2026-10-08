import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import numpy as np
from fastapi.testclient import TestClient

from main import app
from regional_weather import RegionalWeather
from route_weather import align_route, route_positions, segment_distance
from weather_service import WeatherUnavailable
from test_live_weather import METADATA, NOW, RAW, model


def temporal_payload():
    snapshots = []
    init = datetime(2026, 10, 8, tzinfo=timezone.utc)
    for lead in (0, 6, 12, 18, 24):
        fields = {**RAW, 't2m_K': 280 + lead, 'tp_6h_mm': lead if lead else 999}
        values = [np.full((2, 2), value).tolist() for value in fields.values()]
        snapshots.append({'valid_time_utc': (init + timedelta(hours=lead)).isoformat(),
                          'lead_hours': lead, 'values': values})
    return {'latitudes': [31.75, 31.5], 'longitudes': [119.5, 119.75],
            'channels': ['T2M', 'U10', 'V10', 'MSL', 'TP'], 'snapshots': snapshots}


class RouteWeatherTests(unittest.TestCase):
    def grid(self):
        return RegionalWeather(temporal_payload(), METADATA['init_time_utc'], 'NOAA GFS')

    def test_time_interpolation_never_interpolates_six_hour_rain(self):
        grid = self.grid()
        for hour, rain in [(0, 6), (3, 6), (6, 6), (9, 12), (24, 24)]:
            stamp = (grid.init + timedelta(hours=hour)).isoformat()
            sampled = grid.sample(31.625, 119.625, valid_time_utc=stamp)
            self.assertEqual(sampled['values']['t2m_K'], 280 + hour)
            self.assertEqual(sampled['values']['tp_6h_mm'], rain)
            self.assertFalse(sampled['rain_interval']['represents_instantaneous_intensity'])
            self.assertEqual(sampled['time_method'], 'native_time' if hour % 6 == 0 else 'linear_time_estimate')
        self.assertEqual(grid.sample(31.5, 119.5, valid_time_utc=grid.init.isoformat())['sources']['t2m_K'], 'initial_background')

    def test_no_time_extrapolation_or_interpolation_across_missing_step(self):
        grid = self.grid()
        for hour in (-1, 25):
            with self.assertRaises(WeatherUnavailable):
                grid.sample(31.5, 119.5, valid_time_utc=(grid.init + timedelta(hours=hour)).isoformat())
        payload = temporal_payload()
        payload['snapshots'].pop(2)
        grid = RegionalWeather(payload, METADATA['init_time_utc'], 'NOAA GFS')
        with self.assertRaises(WeatherUnavailable): grid.sample(31.5, 119.5, valid_time_utc='2026-10-08T09:00:00Z')

    def test_corrector_not_applied_to_initial_intermediate_or_unsupported_lead(self):
        grid = self.grid()
        for hour in (0, 3, 6, 12, 18, 24):
            result = grid.sample(31.5, 119.5, model(), (grid.init + timedelta(hours=hour)).isoformat())
            self.assertEqual(result['corrector_applicable'], hour in (6, 12, 24))
            for field in ('v10_m_s', 'msl_Pa'): self.assertEqual(result['values'][field], result['raw'][field])

    def test_route_follows_vertices_and_splits_long_segments(self):
        coords = [[119.51, 31.51], [119.6, 31.51], [119.6, 31.6]]
        positions, total = route_positions(coords)
        self.assertTrue(any(p['coordinate'] == coords[1] for p in positions))
        self.assertEqual(positions[0]['coordinate'], coords[0])
        self.assertEqual(positions[-1]['coordinate'], coords[-1])
        self.assertAlmostEqual(total, segment_distance(coords[0], coords[1]) + segment_distance(coords[1], coords[2]))
        for a, b in zip(positions, positions[1:]): self.assertLessEqual(b['distance_m'] - a['distance_m'], 1000.01)

    def test_arrival_times_are_cumulative_distance_not_a_single_forecast_time(self):
        result = align_route(self.grid(), [[119.51, 31.51], [119.6, 31.6]], '2026-10-08T11:00:00+08:00', 45, 100, now=NOW)
        self.assertEqual(result['state'], 'time_aligned_surface_only')
        self.assertEqual(result['departure_time_utc'], '2026-10-08T03:00:00Z')
        self.assertTrue(result['coverage']['surface_space_time'])
        self.assertFalse(result['coverage']['flight_height'])
        self.assertIsNone(result['flight_permission'])
        first = datetime.fromisoformat(result['samples'][0]['eta_utc'])
        for sample in result['samples']:
            self.assertAlmostEqual((datetime.fromisoformat(sample['eta_utc']) - first).total_seconds(), sample['distance_m'] / (45 / 3.6), places=5)
        self.assertNotEqual(result['samples'][0]['weather']['values']['t2m_K'], result['samples'][-1]['weather']['values']['t2m_K'])

    def test_partial_temporal_or_spatial_coverage_is_explicit(self):
        for coords, departure in [([[119.51, 31.51], [119.6, 31.6]], '2026-10-08T23:59:00Z'),
                                  ([[119.51, 31.51], [119.8, 31.6]], '2026-10-08T03:00:00Z')]:
            result = align_route(self.grid(), coords, departure, 45, 100, now=NOW)
            self.assertEqual(result['state'], 'incomplete')
            self.assertTrue(result['missing'])
            self.assertFalse(result['coverage']['surface_space_time'])
            self.assertIn('未完全匹配', result['message'])

    def test_invalid_departure_or_route_rejected(self):
        for departure in ('2026-10-08T03:00:00', '2026-10-08T00:00:00Z'):
            with self.assertRaises(WeatherUnavailable):
                align_route(self.grid(), [[119.51, 31.51], [119.6, 31.6]], departure, 45, 100, now=NOW)
        for coords in ([[119.5, 31.5], [119.5, 31.5]], [[float('nan'), 31.5], [119.6, 31.5]]):
            with self.assertRaises(WeatherUnavailable): route_positions(coords)

    def test_route_api_calls_job_and_rejects_invalid_speed(self):
        manager = Mock()
        manager.route.return_value = {'state': 'time_aligned_surface_only'}
        payload = {'coordinates': [[119.51, 31.51], [119.6, 31.6]], 'departure_time_utc': '2026-10-08T03:00:00Z', 'speed_kmh': 45, 'altitude_m': 100}
        with patch('main.forecast_jobs', manager):
            client = TestClient(app)
            self.assertEqual(client.post('/api/weather/predictions/test/route', json=payload).status_code, 200)
            self.assertEqual(manager.route.call_args.args[0], 'test')
            self.assertEqual(client.post('/api/weather/predictions/test/route', json={**payload, 'speed_kmh': 0}).status_code, 422)
            manager.route.side_effect = KeyError('test')
            self.assertEqual(client.post('/api/weather/predictions/test/route', json=payload).status_code, 404)


if __name__ == '__main__':
    unittest.main()

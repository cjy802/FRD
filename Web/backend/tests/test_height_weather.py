import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import numpy as np

from height_weather import CHANNELS, HEIGHTS, HeightWeather
from live_weather import ForecastJobs
from regional_weather import RegionalWeather
from route_weather import align_route
from test_live_weather import METADATA, NOW, RAW, model, wait_for_job
from test_route_weather import temporal_payload
from weather_service import WeatherUnavailable


def height_payload():
    init = datetime(2026, 10, 8, tzinfo=timezone.utc)
    snapshots = []
    for lead in (0, 6, 12, 18, 24):
        values = []
        for channel in CHANNELS:
            value = 10 if channel == 'GUST' else 10000 if channel == 'VIS' else int(channel[1:]) / 10 + lead if channel[0] == 'U' else 0
            values.append(np.full((2, 2), value).tolist())
        snapshots.append({'lead_hours': lead, 'valid_time_utc': (init + timedelta(hours=lead)).isoformat(), 'values': values})
    return {'init_time_utc': init.isoformat(), 'source_cycle_utc': init.isoformat(), 'height_reference': 'AGL',
            'heights_m': list(HEIGHTS), 'channels': list(CHANNELS), 'latitudes': [31.75, 31.5],
            'longitudes': [119.5, 119.75], 'snapshots': snapshots}


class HeightWeatherTests(unittest.TestCase):
    def grid(self): return HeightWeather(height_payload(), METADATA['init_time_utc'])

    def test_direct_and_interpolated_height_wind_with_time_estimate(self):
        a = self.grid().sample_height(31.625, 119.625, 100, '2026-10-08T06:00:00Z')
        self.assertEqual(a['wind_speed_m_s'], 16)
        self.assertEqual(a['height_method'], 'native_height')
        b = self.grid().sample_height(31.625, 119.625, 65, '2026-10-08T03:00:00Z')
        self.assertEqual(b['wind_speed_m_s'], 9.5)
        self.assertEqual(b['height_bracket_m'], [50, 80])
        self.assertEqual(b['time_method'], 'linear_time_estimate')
        self.assertEqual(b['height_method'], 'linear_height_estimate')
        self.assertEqual(b['gust_surface_m_s'], 10)
        self.assertEqual(b['visibility_surface_m'], 10000)
        self.assertIn('地面指标', b['warning'])

    def test_no_height_or_time_extrapolation_or_coordinate_snapping(self):
        for altitude, stamp, lat in [(9, '2026-10-08T06:00:00Z', 31.5), (101, '2026-10-08T06:00:00Z', 31.5),
                                    (100, '2026-10-09T01:00:00Z', 31.5), (100, '2026-10-08T06:00:00Z', 30)]:
            with self.assertRaises(WeatherUnavailable): self.grid().sample_height(lat, 119.5, altitude, stamp)

    def test_wrong_reference_epoch_negative_hazards_or_stale_source_rejected(self):
        for kind in ('reference', 'epoch', 'negative', 'stale'):
            payload = copy.deepcopy(height_payload())
            if kind == 'reference': payload['height_reference'] = 'MSL'
            if kind == 'epoch': payload['init_time_utc'] = '2026-10-07T00:00:00Z'
            if kind == 'negative': payload['snapshots'][0]['values'][-1][0][0] = -1
            if kind == 'stale': payload['source_cycle_utc'] = '2026-10-06T00:00:00Z'
            with self.assertRaises(WeatherUnavailable): HeightWeather(payload, METADATA['init_time_utc'])

    def test_route_height_coverage_and_unsupported_height_not_replaced_by_10m(self):
        surface = RegionalWeather(temporal_payload(), METADATA['init_time_utc'], 'NOAA GFS')
        for altitude in (100, 120):
            result = align_route(surface, [[119.51, 31.51], [119.6, 31.6]], '2026-10-08T03:00:00Z', 45, altitude, height_grid=self.grid())
            self.assertEqual(result['coverage']['flight_height'], altitude == 100)
            self.assertEqual(result['coverage']['surface_space_time'], True)
            self.assertEqual(result['samples'][0]['height_weather'] is not None, altitude == 100)
            if altitude == 120: self.assertIn('不进行高度外推', result['height_missing'][0]['reason'])

    def test_auxiliary_failure_preserves_surface_forecast_and_explains_missing_height(self):
        worker = lambda path, inspect: METADATA if inspect else {**METADATA, 'raw': RAW, 'region': temporal_payload()}
        for available in (True, False):
            def provider(path):
                if not available: raise WeatherUnavailable('Mock NOAA unavailable')
                return height_payload()
            manager = ForecastJobs(worker=worker, clock=lambda: NOW, corrector=lambda path: model(), preparer=lambda path: path, height_provider=provider)
            self.addCleanup(manager.executor.shutdown, wait=True)
            with patch('live_weather.Path.is_file', return_value=True): result = wait_for_job(manager, manager.start())
            self.assertEqual(result['status'], 'succeeded')
            self.assertEqual(result['forecast']['height_status'], 'available' if available else 'unavailable')
            aligned = manager.route(result['job_id'], [[119.51, 31.51], [119.6, 31.6]], '2026-10-08T03:00:00Z', 45, 100)
            self.assertEqual(aligned['coverage']['flight_height'], available)


if __name__ == '__main__': unittest.main()

import unittest
import json
import math

from height_weather import HeightWeather
from regional_weather import RegionalWeather
from route_weather import align_route
from test_height_weather import height_payload
from test_live_weather import METADATA
from test_route_weather import temporal_payload
from weather_risk import DEMO_LIMITS, bearing_degrees, resolve_limits, screen_route, screen_values, wind_components
from weather_service import WeatherUnavailable


class WeatherRiskTests(unittest.TestCase):
    def alignment(self, height=True):
        coordinates = [[119.51, 31.51], [119.6, 31.6]]
        grid = RegionalWeather(temporal_payload(), METADATA['init_time_utc'], 'NOAA GFS')
        auxiliary = HeightWeather(height_payload(), METADATA['init_time_utc']) if height else None
        return align_route(grid, coordinates, '2026-10-08T03:00:00Z', 45, 100, height_grid=auxiliary), coordinates

    def test_wind_vectors_project_to_route_direction_not_meteorological_from_direction(self):
        self.assertAlmostEqual(bearing_degrees([119, 31], [119, 32]), 0)
        self.assertAlmostEqual(bearing_degrees([119, 0], [120, 0]), 90)
        self.assertAlmostEqual(wind_components(5, 0, 90)['tailwind_m_s'], 5)
        self.assertAlmostEqual(wind_components(5, 0, 270)['headwind_m_s'], 5)
        self.assertAlmostEqual(wind_components(5, 0, 0)['crosswind_m_s'], 5)
        self.assertAlmostEqual(wind_components(0, -5, 0)['headwind_m_s'], 5)

    def test_thresholds_visibility_and_zero_visibility_are_finite(self):
        for value, level in [(9.5, 'no_trigger'), (9.6, 'attention'), (12, 'elevated')]:
            self.assertEqual(screen_values({'wind': value}, DEMO_LIMITS)['level'], level)
        for value, level in [(4000, 'no_trigger'), (3750, 'attention'), (3000, 'elevated'), (0, 'elevated')]:
            result = screen_values({'visibility': value}, DEMO_LIMITS)
            self.assertEqual(result['level'], level)
            self.assertLessEqual(result['score_0_100'], 100)
            if result['triggers']: self.assertEqual(result['triggers'][0]['operator'], '≤')

    def test_route_reports_location_direction_reason_and_custom_limits(self):
        aligned, coordinates = self.alignment()
        risk = screen_route(aligned, coordinates)
        self.assertEqual(risk['level'], 'elevated')
        self.assertTrue(risk['coverage_complete'])
        self.assertIsNone(risk['flight_permission'])
        self.assertEqual(len(risk['segments']), aligned['sample_count'] - 1)
        self.assertTrue(all(segment['covered'] for segment in risk['segments']))
        for alert in risk['alerts']:
            self.assertEqual(len(alert['coordinate']), 2)
            self.assertIn('eta_utc', alert)
            self.assertIn(alert['label'], alert['reason'])
        profile = {**DEMO_LIMITS, 'max_wind_m_s': 100, 'max_crosswind_m_s': 100, 'max_headwind_m_s': 100,
                   'max_gust_m_s': 100, 'max_rain_6h_mm': 100, 'min_visibility_m': 1}
        relaxed = screen_route(aligned, coordinates, profile)
        self.assertEqual(relaxed['level'], 'no_trigger')
        self.assertEqual(relaxed['rule_mode'], 'custom_unverified')
        self.assertIsNone(relaxed['flight_permission'])

    def test_missing_height_is_unassessed_not_zero_wind_or_a_pass(self):
        aligned, coordinates = self.alignment(False)
        risk = screen_route(aligned, coordinates)
        self.assertEqual(risk['level'], 'incomplete')
        self.assertIsNone(risk['score_0_100'])
        self.assertIsNone(risk['summary']['max_wind_m_s'])
        self.assertFalse(risk['coverage_complete'])
        self.assertTrue(all(not segment['covered'] for segment in risk['segments']))

    def test_invalid_or_incomplete_custom_limits_rejected(self):
        for profile in ({}, {**DEMO_LIMITS, 'max_wind_m_s': 0}, {**DEMO_LIMITS, 'min_visibility_m': float('nan')},
                        {**DEMO_LIMITS, 'max_gust_m_s': True}):
            with self.assertRaises(WeatherUnavailable): resolve_limits(profile)

    def test_submetre_visibility_and_extreme_positive_limits_remain_correct_and_serializable(self):
        result = screen_values({'visibility': .4}, {**DEMO_LIMITS, 'min_visibility_m': .5})
        self.assertEqual(result['level'], 'elevated')
        self.assertEqual(result['score_0_100'], 62.5)
        extreme = screen_values({'wind': 4, 'visibility': 0}, {**DEMO_LIMITS, 'max_wind_m_s': 1e-300})
        self.assertTrue(all(math.isfinite(trigger['ratio']) for trigger in extreme['triggers']))
        json.dumps(extreme, allow_nan=False)


if __name__ == '__main__': unittest.main()

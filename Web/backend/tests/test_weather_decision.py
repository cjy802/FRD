import unittest

from height_weather import HeightWeather
from regional_weather import RegionalWeather
from route_weather import align_route
from test_height_weather import height_payload
from test_live_weather import METADATA
from test_route_weather import temporal_payload
from weather_decision import build_decision
from weather_risk import DEMO_LIMITS, screen_route


class DecisionTests(unittest.TestCase):
    def assessment(self, height=True, profile=None):
        coordinates = [[119.51, 31.51], [119.6, 31.6]]
        grid = RegionalWeather(temporal_payload(), METADATA['init_time_utc'], 'NOAA GFS')
        result = align_route(grid, coordinates, '2026-10-08T03:00:00Z', 45, 100,
                             height_grid=HeightWeather(height_payload(), METADATA['init_time_utc']) if height else None)
        result['weather_risk'] = screen_route(result, coordinates, profile)
        return result

    def test_every_weather_level_still_has_unresolved_conditions_and_no_permission(self):
        limits = {**DEMO_LIMITS, 'max_crosswind_m_s': 100, 'max_headwind_m_s': 100,
                  'max_gust_m_s': 100, 'max_rain_6h_mm': 100, 'min_visibility_m': 1}
        cases = [(self.assessment(False), 'incomplete'), (self.assessment(), 'elevated'),
                 (self.assessment(profile={**limits, 'max_wind_m_s': 14}), 'attention'),
                 (self.assessment(profile={**limits, 'max_wind_m_s': 100}), 'no_trigger')]
        for assessment, level in cases:
            decision = build_decision(assessment)
            self.assertEqual(decision['status'], level)
            self.assertIsNone(decision['flight_permission'])
            self.assertIsNone(decision['may_fly'])
            checks = {check['key']: check for check in decision['checks']}
            self.assertEqual(checks['airspace']['state'], 'not_verified')
            self.assertEqual(checks['obstacles']['state'], 'not_verified')
            self.assertEqual(checks['other_weather']['state'], 'not_covered')

    def test_user_limits_are_not_treated_as_verified_aircraft_limits(self):
        demo = build_decision(self.assessment())
        custom = build_decision(self.assessment(profile=DEMO_LIMITS))
        self.assertEqual(next(x for x in demo['checks'] if x['key'] == 'aircraft')['state'], 'not_configured')
        self.assertEqual(next(x for x in custom['checks'] if x['key'] == 'aircraft')['state'], 'provided_unverified')


if __name__ == '__main__': unittest.main()

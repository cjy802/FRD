import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from main import app
from weather_service import WeatherUnavailable
from weather_windows import compare_departures


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.init = datetime(2026, 10, 8, tzinfo=timezone.utc)
        self.times = [self.init + timedelta(hours=h) for h in (0, 6, 12, 18, 24)]
        self.coords = [[119.51, 31.51], [119.6, 31.6]]

    def compare(self, evaluator, selected='2026-10-08T03:00:00Z'):
        return compare_departures(evaluator, self.times, self.coords, selected, 45, self.times[-1], self.init + timedelta(hours=1))

    def evaluation(self, score=10, covered=True, level='no_trigger'):
        return {'missing': [], 'height_missing': [] if covered else [{'reason': '缺高度资料'}],
                'weather_risk': {'coverage_complete': covered, 'level': level if covered else 'incomplete',
                                 'label': 'test', 'score_0_100': score if covered else None, 'summary': {}}}

    def test_native_slots_rank_with_selected_and_exclude_mission_beyond_end(self):
        evaluated = []
        def evaluate(stamp):
            evaluated.append(stamp)
            return self.evaluation(5 if datetime.fromisoformat(stamp).hour == 12 else 10)
        result = self.compare(evaluate)
        self.assertEqual(result['state'], 'available')
        self.assertEqual(result['covered_count'], 4)
        self.assertEqual(datetime.fromisoformat(result['best_candidate']['departure_time_utc']).hour, 12)
        excluded = next(item for item in result['candidates'] if datetime.fromisoformat(item['departure_time_utc']).hour == 0)
        self.assertFalse(excluded['covered'])
        self.assertIn('完整任务', excluded['reason'])
        self.assertEqual(len(evaluated), 4)
        self.assertIsNone(result['flight_permission'])

    def test_no_height_data_never_produces_a_preferred_flyable_slot(self):
        result = self.compare(lambda stamp: self.evaluation(covered=False))
        self.assertEqual(result['state'], 'no_covered_candidate')
        self.assertIsNone(result['best_candidate'])
        self.assertTrue(all(item['score_0_100'] is None for item in result['candidates']))

    def test_past_selected_and_failed_slot_are_explicit_not_replaced_silently(self):
        def evaluate(stamp):
            if datetime.fromisoformat(stamp).hour == 12: raise WeatherUnavailable('Mock data missing')
            return self.evaluation()
        result = self.compare(evaluate, '2026-10-08T00:00:00Z')
        self.assertTrue(any('已过去' in item['reason'] for item in result['candidates']))
        self.assertTrue(any('Mock data missing' in item['reason'] for item in result['candidates']))

    def test_invalid_timezone_or_speed_rejected(self):
        with self.assertRaises(WeatherUnavailable): self.compare(lambda stamp: self.evaluation(), '2026-10-08T03:00:00')
        with self.assertRaises(WeatherUnavailable):
            compare_departures(None, self.times, self.coords, '2026-10-08T03:00:00Z', 0, self.times[-1], self.init)

    def test_window_api_and_unknown_job(self):
        manager = Mock()
        manager.windows.return_value = {'state': 'available'}
        payload = {'coordinates': self.coords, 'departure_time_utc': '2026-10-08T03:00:00Z'}
        with patch('main.forecast_jobs', manager):
            client = TestClient(app)
            self.assertEqual(client.post('/api/weather/predictions/test/windows', json=payload).status_code, 200)
            manager.windows.side_effect = KeyError('test')
            self.assertEqual(client.post('/api/weather/predictions/test/windows', json=payload).status_code, 404)


if __name__ == '__main__': unittest.main()

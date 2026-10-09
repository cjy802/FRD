import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from live_weather import prepare_height_weather
from weather_service import WeatherUnavailable


class HeightProcessTests(unittest.TestCase):
    def completed(self, code=0, stdout='', stderr=''):
        return subprocess.CompletedProcess([], code, stdout, stderr)

    def test_native_crash_preserves_exit_code_and_stderr_in_message(self):
        result = self.completed(3221225477, '', 'Windows fatal exception: access violation\nNative decoder stack')
        with patch('live_weather.subprocess.run', return_value=result) as run:
            with self.assertRaises(WeatherUnavailable) as context: prepare_height_weather(Path('input.nc'))
        self.assertIn('0xC0000005', str(context.exception))
        self.assertIn('access violation', str(context.exception))
        self.assertIn('faulthandler', run.call_args.args[0])

    def test_malformed_json_and_wrong_structure_are_explicit(self):
        for stdout in ('unexpected output', '[]', 'null', '42'):
            with patch('live_weather.subprocess.run', return_value=self.completed(stdout=stdout)):
                with self.assertRaises(WeatherUnavailable): prepare_height_weather(Path('input.nc'))

    def test_source_error_and_success_payload(self):
        with patch('live_weather.subprocess.run', return_value=self.completed(1, json.dumps({'error': 'missing fields'}), 'decode detail')):
            with self.assertRaises(WeatherUnavailable) as context: prepare_height_weather(Path('input.nc'))
            self.assertIn('missing fields', str(context.exception))
            self.assertIn('decode detail', str(context.exception))
        payload = {'init_time_utc': '2026-10-09T00:00:00Z', 'snapshots': []}
        with patch('live_weather.subprocess.run', return_value=self.completed(stdout=json.dumps(payload))):
            self.assertEqual(prepare_height_weather(Path('input.nc')), payload)

    def test_timeout_returns_readable_height_failure(self):
        with patch('live_weather.subprocess.run', side_effect=subprocess.TimeoutExpired([], 600)):
            with self.assertRaisesRegex(WeatherUnavailable, '600 秒'): prepare_height_weather(Path('input.nc'))


if __name__ == '__main__': unittest.main()

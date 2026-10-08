import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from predict_fuxi_point import forecast_region
from build_fuxi_pairs import time_embedding
from datetime import datetime, timedelta


class RegionalPredictionTests(unittest.TestCase):
    def test_rollout_reuses_output_and_advances_embedding_keeps_first_point(self):
        metadata = {'init_time_utc': '2026-10-08T00:00:00Z', 'valid_time_utc': '2026-10-08T06:00:00Z'}
        initial = np.broadcast_to(np.float32(0), (1, 2, 70, 721, 1440))
        states = [np.broadcast_to(np.float32(i), initial.shape) for i in range(1, 5)]
        session = Mock()
        session.run.side_effect = [[value] for value in states]
        result = forecast_region(session, initial, metadata, 234, 478)
        self.assertEqual([s['lead_hours'] for s in result['region']['snapshots']], [0, 6, 12, 18, 24])
        self.assertEqual(result['region']['snapshots'][-1]['valid_time_utc'], '2026-10-09T00:00:00+00:00')
        self.assertTrue(all(value == 1 for value in result['raw'].values()))
        for i, call in enumerate(session.run.call_args_list):
            self.assertIs(call.args[1]['input'], initial if i == 0 else states[i - 1])
            np.testing.assert_array_equal(call.args[1]['temb'], time_embedding(datetime(2026, 10, 8) + timedelta(hours=i * 6)))

    def test_no_unsupported_horizon(self):
        with self.assertRaises(ValueError): forecast_region(None, None, {}, 0, 0, 7)


if __name__ == '__main__':
    unittest.main()

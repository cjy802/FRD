import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from weather_region import crop_region


class RegionCropTests(unittest.TestCase):
    def test_retains_original_channels_coordinates_and_values(self):
        # Broadcast avoids allocating another full global state in a unit test.
        state = np.broadcast_to(np.arange(70, dtype=np.float32)[:, None, None], (70, 721, 1440))
        region = crop_region(state, '2026-10-08T06:00:00Z', 6)
        self.assertEqual(region['latitudes'][0], 35.5)
        self.assertEqual(region['latitudes'][-1], 30)
        self.assertEqual(region['longitudes'][0], 116)
        self.assertEqual(region['longitudes'][-1], 122.5)
        self.assertEqual(len(region['channels']), 70)
        self.assertEqual(region['snapshots'][0]['values'][69][0][0], 69)

    def test_invalid_global_shape_or_wrapping_region_fails(self):
        with self.assertRaises(ValueError): crop_region(np.zeros((5, 2, 2)), '', 6)
        state = np.broadcast_to(np.float32(1), (70, 721, 1440))
        with self.assertRaises(ValueError): crop_region(state, '', 6, (359, 30, 1, 31))


if __name__ == '__main__':
    unittest.main()

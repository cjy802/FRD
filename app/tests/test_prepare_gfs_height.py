import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prepare_gfs_height import CHANNELS, HEIGHTS, cached_frame_valid, decode_region, select_height_records
import copy
import numpy as np
from datetime import datetime, timezone


def inventory():
    records = []
    for height in HEIGHTS:
        for variable in ('UGRD', 'VGRD'):
            index = len(records)
            records.append({'variable': variable, 'level': f'{height} m above ground', 'start': index * 100, 'end': index * 100 + 99})
    for variable in ('GUST', 'VIS'):
        index = len(records)
        records.append({'variable': variable, 'level': 'surface', 'start': index * 100, 'end': index * 100 + 99})
    return records


class HeightProviderTests(unittest.TestCase):
    def test_explicit_height_fields_without_pressure_level_substitution(self):
        records = select_height_records(inventory())
        self.assertEqual({record['channel'] for record in records}, set(CHANNELS))
        self.assertEqual(len(records), 16)

    def test_missing_or_duplicate_height_channel_is_rejected(self):
        for records in (inventory()[:-1], inventory() + [inventory()[0]]):
            with self.assertRaises(ValueError): select_height_records(records)

    def test_real_cached_regional_grib_optional_without_network(self):
        path = Path(__file__).resolve().parents[1] / 'prepared/gfs_aux_cache/nomads_2026100800_f006.grib2'
        if not path.is_file(): self.skipTest('Live regional GRIB cache not present')
        data = decode_region(path, datetime(2026, 10, 8, 6, tzinfo=timezone.utc))
        self.assertEqual((len(data['values']), len(data['latitudes']), len(data['longitudes'])), (16, 23, 27))
        self.assertEqual((data['latitudes'][0], data['longitudes'][0]), (35.5, 116))

    def test_cached_frame_requires_provenance_complete_grid_and_finite_values(self):
        cycle = datetime(2026, 10, 8, tzinfo=timezone.utc)
        frame = {'generator': 'fuxi-gfs-height', 'schema_version': 1, 'source_cycle_utc': cycle.isoformat(),
                 'valid_time_utc': '2026-10-08T06:00:00+00:00', 'source_forecast_hours': 6,
                 'channels': list(CHANNELS), 'latitudes': np.arange(35.5, 29.9, -.25).tolist(),
                 'longitudes': np.arange(116, 122.6, .25).tolist(), 'values': np.ones((16, 23, 27)).tolist()}
        self.assertTrue(cached_frame_valid(frame, cycle, 6))
        for kind in ('shape', 'value', 'source', 'generator'):
            invalid = copy.deepcopy(frame)
            if kind == 'shape': invalid['values'].pop()
            if kind == 'value': invalid['values'][0][0][0] = float('nan')
            if kind == 'source': invalid['source_forecast_hours'] = 0
            if kind == 'generator': invalid['generator'] = 'foreign-data'
            self.assertFalse(cached_frame_valid(invalid, cycle, 6))


if __name__ == '__main__': unittest.main()

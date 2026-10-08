import io
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import prepare_gfs_live as gfs


def state_records():
    records = []
    for variable in gfs.VARIABLES:
        for level in gfs.LEVELS:
            records.append({"variable": variable, "level": f"{level} mb", "period": "anl"})
    for variable, level in gfs.SURFACE:
        records.append({"variable": variable, "level": level, "period": "anl"})
    return [{**record, "start": i * 100, "end": i * 100 + 99, "cycle": "d=2026100800"} for i, record in enumerate(records)]


class GFSInputTests(unittest.TestCase):
    def test_all_69_state_channels_are_explicitly_selected(self):
        selected = gfs.select_records(state_records(), "state")
        self.assertEqual(len(selected), 69)
        self.assertEqual({record["channel"] for record in selected}, set(gfs.CHANNELS) - {"TP"})

    def test_missing_pressure_fields_cannot_be_filled_with_zeros(self):
        with self.assertRaisesRegex(ValueError, "lacks fields"):
            gfs.select_records(state_records()[1:], "state")

    def test_six_hour_rain_selects_one_accumulator_not_both(self):
        base = {"variable": "APCP", "level": "surface", "period": "0-6 hour acc fcst", "cycle": "d=2026100800"}
        records = [{**base, "start": 10, "end": 19}, {**base, "start": 20, "end": 29}]
        selected = gfs.select_records(records, "rain")
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["start"], 10)
        with self.assertRaisesRegex(ValueError, "lacks fields"):
            gfs.select_records([{**base, "period": "0-3 hour acc fcst", "start": 0, "end": 9}], "rain")

    def test_inventory_has_exact_byte_boundaries(self):
        records = gfs.parse_inventory("1:0:d=2026100800:TMP:2 m above ground:anl:\n2:40:d=2026100800:MSLET:mean sea level:anl:")
        self.assertEqual(records[0]["end"], 39)
        self.assertIsNone(records[1]["end"])
        with self.assertRaises(ValueError):
            gfs.parse_inventory("1:40:d=2026100800:TMP:2 m above ground:anl:\n2:0:d=2026100800:MSLET:mean sea level:anl:")

    def test_current_frame_is_six_hourly_without_future_dated_origin(self):
        now = datetime(2026, 10, 8, 5, 59, tzinfo=timezone.utc)
        self.assertEqual(gfs.six_hour_time(now), datetime(2026, 10, 8, 0, tzinfo=timezone.utc))

    def test_server_cannot_force_full_file_download(self):
        downloader = gfs.GFSDownloader(Path("unused"))
        downloader.inventory = Mock(return_value=state_records())
        response = MagicMock()
        response.status_code = 200
        response.__enter__.return_value = response
        downloader.session.get = Mock(return_value=response)
        with patch.object(Path, "is_file", return_value=False), patch.object(Path, "mkdir"), \
                patch.object(Path, "open", return_value=io.BytesIO()), patch("prepare_gfs_live.time.sleep"):
            with self.assertRaisesRegex(ValueError, "full-file download"):
                downloader.fetch_fields(datetime(2026, 10, 8, tzinfo=timezone.utc), 0, "state")
        response.iter_content.assert_not_called()


if __name__ == "__main__":
    unittest.main()

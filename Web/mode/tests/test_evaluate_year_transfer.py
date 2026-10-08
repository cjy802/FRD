"""Checks for deduplication and forward-year evaluation boundaries."""

import sys
import unittest
from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import evaluate_year_transfer as transfer


def sample_year(year, rows=12):
    times = pd.date_range(f"{year}-01-01T06:00:00Z", periods=rows, freq="D")
    table = pd.DataFrame({
        "init_time_utc": times,
        "valid_time_utc": times + pd.Timedelta(hours=6),
        "lead_hours": 6, "latitude_deg": 31.5, "longitude_deg": 119.5,
    })
    for index, field in enumerate(transfer.tc.FIELDS):
        values = np.arange(rows, dtype=float) + index
        table[f"fuxi_{field}"] = values
        table[f"era5_{field}"] = values + np.sin(values) * 0.1
    return table[transfer.COMMON_COLUMNS]


class YearTransferTests(unittest.TestCase):
    def test_overlap_is_counted_once_and_conflicts_are_rejected(self):
        history = pd.concat([sample_year(2024), sample_year(2025)], ignore_index=True)
        features = sample_year(2025)
        combined, overlap = transfer.merge_sources(history, features)
        self.assertEqual(overlap, 12)
        self.assertEqual(len(combined), 24)
        features.loc[0, "era5_t2m_K"] += 1
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            transfer.merge_sources(history, features)

    def test_cross_year_labels_are_purged_and_init_groups_do_not_overlap(self):
        boundary = sample_year(2025, 1)
        boundary["init_time_utc"] = pd.Timestamp("2025-12-31T18:00:00Z")
        boundary["valid_time_utc"] = pd.Timestamp("2026-01-01T06:00:00Z")
        boundary["lead_hours"] = 12
        table = pd.concat([sample_year(2024), sample_year(2025), boundary, sample_year(2026)])
        train, test, purged = transfer.split_years(table, [2024, 2025], 2026)
        self.assertEqual(purged, 1)
        self.assertEqual(len(train), 24)
        self.assertEqual(len(test), 12)
        self.assertFalse(set(train.init_time_utc) & set(test.init_time_utc))
        self.assertLess(train.valid_time_utc.max(), test.init_time_utc.min())

    def test_future_training_year_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "precede"):
            transfer.split_years(sample_year(2025), [2024, 2026], 2025)

    def test_test_labels_cannot_change_model_or_scalers(self):
        train = pd.concat([sample_year(2024), sample_year(2025)], ignore_index=True)
        test = sample_year(2026)
        model1, _, predictions, months, _ = transfer.evaluate(train, test, 10)
        changed = test.copy()
        for field in transfer.tc.FIELDS:
            changed[f"era5_{field}"] += 100
        model2, _, _, _, _ = transfer.evaluate(train, changed, 10)
        for name in ["coefficients", "feature_mean", "feature_scale", "target_mean", "target_scale"]:
            np.testing.assert_array_equal(model1[name], model2[name])
        self.assertEqual(len(predictions), len(test))
        self.assertEqual(len(months), len(transfer.tc.FIELDS))

    def test_unused_rich_features_need_no_imputation(self):
        table = sample_year(2025)
        table["feature_not_available"] = np.nan
        loaded = transfer.load_common_table(StringIO(table.to_csv(index=False)))
        self.assertEqual(len(loaded), len(table))
        self.assertNotIn("feature_not_available", loaded.columns)

    def test_invalid_forecast_time_alignment_is_rejected(self):
        table = sample_year(2025)
        table.loc[0, "valid_time_utc"] += pd.Timedelta(hours=1)
        with self.assertRaisesRegex(ValueError, "does not equal"):
            transfer.load_common_table(StringIO(table.to_csv(index=False)))


if __name__ == "__main__":
    unittest.main()

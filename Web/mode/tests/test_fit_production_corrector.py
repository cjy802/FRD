import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fit_production_corrector as production
import train_corrector as tc
from test_evaluate_year_transfer import sample_year


class ProductionFitTests(unittest.TestCase):
    def test_all_years_including_last_rows_contribute_to_fitting(self):
        data = pd.concat([sample_year(2024), sample_year(2025), sample_year(2026)], ignore_index=True)
        data.loc[data.init_time_utc.dt.year.eq(2026), "era5_t2m_K"] += 3
        model, names = production.fit_all_rows(data, 10)
        x, expected_names = tc.make_features(data)
        y = tc.residual_targets(data)
        expected = tc.fit_ridge(x, y, 10)
        self.assertEqual(names, expected_names)
        for field in ("coefficients", "feature_mean", "feature_scale", "target_mean", "target_scale"):
            np.testing.assert_array_equal(model[field], expected[field])
        self.assertAlmostEqual(model["target_mean"][0], y[:, 0].mean())

    def test_rich_only_columns_are_not_silently_used(self):
        data = sample_year(2025)
        data["feature_rich_only"] = 0.0
        with self.assertRaisesRegex(ValueError, "common eight"):
            production.fit_all_rows(data)


if __name__ == "__main__":
    unittest.main()

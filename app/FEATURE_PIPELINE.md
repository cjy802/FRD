# Feature-rich FuXi/ERA5 backfill

`backfill_batches.py` now has an optional feature mode. It downloads the same
global ERA5 inputs, runs the frozen FuXi short model, extracts a small vector
around `31.5N, 119.5E`, and appends the calculated rows to a separate CSV.
Global forecast fields are not saved.

The feature extractor writes:

- six-hour analysis tendencies for the five surface channels;
- center, mean, standard deviation, north-south gradient, and east-west
  gradient for the requested 3x3 or 5x5 FuXi neighbourhood;
- FuXi values and horizontal gradients at 925, 850, and 700 hPa for `Z/T/U/V/R`;
- 925--700 hPa wind shear;
- FuXi, ERA5, and residual values for each requested lead.

The default feature-mode initialization hours are `06 12 18` and the default
leads are `6 12 24`. Initialization hour `00` needs the previous day's input
frame and is rejected by the extractor until the previous-day archive handling
is explicitly enabled. Forecast rows whose ERA5 verification time is outside
the monthly observation file are skipped; this mainly affects the final one or
two days of a month for longer leads.

Example for one month:

```powershell
& 'D:\anaconda\envs\fuxi_onnx\python.exe' `
  'D:\低空经济\app\backfill_batches.py' `
  --year 2025 --months 8 `
  --pressure-times 00:00 06:00 12:00 18:00 `
  --feature-only `
  --feature-output 'D:\低空经济\app\point_data\fuxi_era5_features.csv' `
  --feature-init-hours 6 12 18 `
  --feature-lead-hours 6 12 24 `
  --feature-neighborhood-size 3 `
  --execute
```

Use `--feature-neighborhood-size 5` for a 5x5 window. The feature CSV is
separate from `fuxi_era5_pairs_6h.csv` because multi-lead and multi-init rows
can share the same `valid_time_utc`. The feature CSV can be passed to
`Web/mode/train_corrector.py`; columns whose names start with `feature_` are
automatically added to the model input.

## Train on 2024/2025 and evaluate 2026

The old point-pair table includes 2024 and 2026, but does not include the rich
predictors. `Web/mode/evaluate_year_transfer.py` combines both tables using
only their eight common predictors, verifies agreement of overlapping records,
and removes duplicate `(init_time, lead, latitude, longitude)` rows.

```powershell
& 'D:\anaconda\envs\fuxi_onnx\python.exe' `
  'D:\低空经济\Web\mode\evaluate_year_transfer.py' `
  --data 'D:\低空经济\app\point_data\fuxi_era5_features.csv' `
  --history-data 'D:\低空经济\app\point_data\fuxi_era5_pairs_6h.csv' `
  --train-years 2024 2025 `
  --test-year 2026 `
  --alpha 10 `
  --output 'D:\低空经济\Web\mode\output_year_transfer_2026' `
  --train
```

Omit `--train` for a read-only preview. Training labels ending in the test year
are excluded even if their initialization belongs to an earlier year. The saved
`corrector_ridge.npz` is fitted on training years only, not refitted on 2026.
Outputs include `metrics.json`, `test_predictions.csv`, `monthly_metrics.csv`,
and `seasonal_metrics.csv`. Grouped scores pool errors before calculating RMSE.

This is a retrospective evaluation against available ERA5 pairs, not new global
FuXi inference. The test table currently has only 06 UTC initializations and
+6-hour leads, and its available dates end on 2026-09-06. Missing rich predictors
are not filled with zero, and this experiment does not validate the 84-feature
model. Completing 2026 rich features is necessary for that separate comparison.

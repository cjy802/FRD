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

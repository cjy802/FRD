"""Join regional FuXi-2.1/GFS backfill forecasts with ERA5 box truth.

Reads the forecast table written by ``backfill_gfs_fuxi.py`` and every
monthly truth CSV in the ``era5_region`` directory written by
``download_era5_region.py``, matches rows on (valid_time, latitude,
longitude) and writes the pair table consumed by ``Web/mode/train_corrector.py``:
``fuxi_*`` predictors, ``era5_*`` labels, ``residual_*`` targets.

Forecast rows whose verification time is missing from the truth tables are
dropped and counted, not zero-filled. The join keeps forecast table memory
bounded by reading truth files one by one and concatenating.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FORECAST = PROJECT_ROOT / "FuXi/DATA/fuxi2_gfs_region_forecast.csv"
DEFAULT_TRUTH_DIR = PROJECT_ROOT / "FuXi/DATA/era5_region"
DEFAULT_OUTPUT = PROJECT_ROOT / "FuXi/DATA/fuxi2_gfs_era5_region_pairs.csv"
FIELDS = ("t2m_K", "u10_m_s", "v10_m_s", "msl_Pa", "tp_6h_mm")
PAIR_COLUMNS = (
    "init_time_utc", "valid_time_utc", "lead_hours",
    "latitude_deg", "longitude_deg", "model",
) + tuple(
    f"{prefix}_{field}"
    for field in FIELDS
    for prefix in ("fuxi", "era5", "residual")
)


def load_truth(truth_dir: Path) -> pd.DataFrame:
    frames = []
    for path in sorted(truth_dir.glob("era5_surface_box_*.csv")):
        frame = pd.read_csv(path, usecols=list(("valid_time_utc", "latitude_deg", "longitude_deg") + FIELDS))
        frames.append(frame)
        print(f"truth: {path.name} -> {len(frame)} rows")
    if not frames:
        raise SystemExit(f"no era5_surface_box_*.csv under {truth_dir}; run download_era5_region.py first")
    truth = pd.concat(frames, ignore_index=True)
    truth = truth.drop_duplicates(subset=["valid_time_utc", "latitude_deg", "longitude_deg"], keep="first")
    truth["latitude_deg"] = truth["latitude_deg"].round(2)
    truth["longitude_deg"] = truth["longitude_deg"].round(2)
    return truth


def load_forecast(forecast: Path) -> pd.DataFrame:
    """Read one forecast CSV or every fuxi2_gfs_region_forecast*.csv beside it."""
    if forecast.is_file():
        paths = [forecast]
    else:
        paths = sorted(forecast.parent.glob(f"{forecast.stem}*.csv"))
        if not paths:
            raise SystemExit(f"no {forecast.stem}*.csv under {forecast.parent}; run backfill_gfs_fuxi.py first")
    frames = []
    for path in paths:
        frame = pd.read_csv(path)
        frames.append(frame)
        print(f"forecast: {path.name} -> {len(frame)} rows")
    table = pd.concat(frames, ignore_index=True)
    return table.drop_duplicates(
        subset=["init_time_utc", "lead_hours", "latitude_deg", "longitude_deg"], keep="first")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--forecast", type=Path, default=DEFAULT_FORECAST)
    parser.add_argument("--truth-dir", type=Path, default=DEFAULT_TRUTH_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    forecast = load_forecast(args.forecast)
    forecast["latitude_deg"] = forecast["latitude_deg"].round(2)
    forecast["longitude_deg"] = forecast["longitude_deg"].round(2)
    print(f"forecast: {len(forecast)} rows, "
          f"{forecast['init_time_utc'].nunique()} inits, leads {sorted(forecast['lead_hours'].unique())}")
    truth = load_truth(args.truth_dir)
    print(f"truth: {len(truth)} rows, {truth['valid_time_utc'].nunique()} valid times")

    merged = forecast.merge(truth, on=["valid_time_utc", "latitude_deg", "longitude_deg"],
                            how="left", validate="many_to_one")
    missing = merged["t2m_K"].isna()
    dropped = int(missing.sum())
    if dropped:
        lost = merged.loc[missing, ["valid_time_utc", "lead_hours"]]
        print(f"dropping {dropped} forecast rows without truth "
              f"({lost['valid_time_utc'].nunique()} valid times); "
              f"first/last: {lost['valid_time_utc'].min()} / {lost['valid_time_utc'].max()}")
    merged = merged.loc[~missing]

    table = pd.DataFrame({
        "init_time_utc": merged["init_time_utc"],
        "valid_time_utc": merged["valid_time_utc"],
        "lead_hours": merged["lead_hours"],
        "latitude_deg": merged["latitude_deg"],
        "longitude_deg": merged["longitude_deg"],
        "model": merged["model"],
    })
    for field in FIELDS:
        table[f"fuxi_{field}"] = merged[f"fuxi_{field}"]
        table[f"era5_{field}"] = merged[field]
        table[f"residual_{field}"] = merged[field] - merged[f"fuxi_{field}"]
    table = table.loc[:, PAIR_COLUMNS]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(args.output, index=False, encoding="utf-8-sig", float_format="%.9g")
    print(f"wrote {len(table)} rows -> {args.output}")
    print(f"grid: {table['latitude_deg'].nunique()} lats x {table['longitude_deg'].nunique()} lons; "
          f"{table['init_time_utc'].nunique()} init dates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

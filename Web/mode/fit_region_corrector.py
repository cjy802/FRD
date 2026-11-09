"""Fit per-grid-cell ridge correctors for the regional service box.

Consumes the pair table written by ``app/pair_gfs_region.py`` (production-style
FuXi-2.1/GFS forecasts verified against ERA5 on the native 0.25-degree box)
and fits, for every grid cell independently, the same standardized ridge on
the deployment eight features (five FuXi surface channels, lead, day-of-year
sine/cosine) toward ERA5 residuals. The saved artifact carries the cell grid
plus stacked coefficient/standardization tensors; ``Web/backend`` resolves the
nearest trained cell at query time.

Cells with fewer than --min-rows training rows stay NaN and fall back to raw
FuXi in production. An optional --test-year reports pooled year-transfer
metrics without touching the saved fit unless --refit-all is given (the
default refits on every row after the evaluation, mirroring the deployment
refit convention).
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA = PROJECT_ROOT / "FuXi/DATA/fuxi2_gfs_era5_region_pairs.csv"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "Web/mode/output_production"
FIELDS = ("t2m_K", "u10_m_s", "v10_m_s", "msl_Pa", "tp_6h_mm")
FEATURE_NAMES = tuple(f"fuxi_{field}" for field in FIELDS) + ("lead_hours", "doy_sin", "doy_cos")
TARGET_NAMES = tuple(f"residual_{field}" for field in FIELDS)


def standardize_fit(values: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mean = values.mean(axis=0)
    scale = values.std(axis=0)
    scale = np.where(scale > 1e-12, scale, 1.0)
    return (values - mean) / scale, mean, scale


def fit_ridge(features: np.ndarray, targets: np.ndarray, alpha: float) -> dict:
    x_scaled, feature_mean, feature_scale = standardize_fit(features)
    y_scaled, target_mean, target_scale = standardize_fit(targets)
    design = np.column_stack((np.ones(len(x_scaled)), x_scaled))
    penalty = np.eye(design.shape[1], dtype=np.float64) * alpha
    left = design.T @ design + penalty
    right = design.T @ y_scaled
    try:
        coefficients = np.linalg.solve(left, right)
    except np.linalg.LinAlgError:
        coefficients = np.linalg.lstsq(left, right, rcond=None)[0]
    return {
        "coefficients": coefficients,
        "feature_mean": feature_mean,
        "feature_scale": feature_scale,
        "target_mean": target_mean,
        "target_scale": target_scale,
    }


def apply_ridge(model: dict, features: np.ndarray) -> np.ndarray:
    scaled = (features - model["feature_mean"]) / model["feature_scale"]
    design = np.column_stack((np.ones(len(scaled)), scaled))
    return design @ model["coefficients"] * model["target_scale"] + model["target_mean"]


def make_features(table: pd.DataFrame) -> np.ndarray:
    timestamps = table["valid_time_utc"]
    day_fraction = (
        timestamps.dt.dayofyear.to_numpy(dtype=np.float64)
        - 1.0
        + timestamps.dt.hour.to_numpy(dtype=np.float64) / 24.0
    )
    angle = 2.0 * np.pi * day_fraction / 365.25
    columns = [table[f"fuxi_{field}"].to_numpy(dtype=np.float64) for field in FIELDS]
    columns.extend((table["lead_hours"].to_numpy(dtype=np.float64), np.sin(angle), np.cos(angle)))
    return np.column_stack(columns)


def residual_targets(table: pd.DataFrame) -> np.ndarray:
    return np.column_stack([
        table[f"era5_{field}"].to_numpy(dtype=np.float64) - table[f"fuxi_{field}"].to_numpy(dtype=np.float64)
        for field in FIELDS
    ])


def load_table(path: Path) -> pd.DataFrame:
    required = {"init_time_utc", "valid_time_utc", "lead_hours", "latitude_deg", "longitude_deg"}
    table = pd.read_csv(path)
    table["valid_time_utc"] = pd.to_datetime(table["valid_time_utc"], utc=True)
    table["init_time_utc"] = pd.to_datetime(table["init_time_utc"], utc=True)
    missing = sorted(required - set(table.columns))
    if missing:
        raise ValueError(f"CSV is missing required columns: {missing}")
    feature_columns = [f"fuxi_{field}" for field in FIELDS] + [f"era5_{field}" for field in FIELDS]
    table = table.dropna(subset=["lead_hours", "latitude_deg", "longitude_deg", *feature_columns])
    return table


def fit_grid(table: pd.DataFrame, alpha: float, min_rows: int) -> tuple[dict, dict]:
    latitudes = np.array(sorted(table["latitude_deg"].unique(), reverse=True), dtype=np.float64)
    longitudes = np.array(sorted(table["longitude_deg"].unique()), dtype=np.float64)
    nlat, nlon = len(latitudes), len(longitudes)
    features = make_features(table)
    targets = residual_targets(table)
    lat_index = {value: index for index, value in enumerate(latitudes)}
    lon_index = {value: index for index, value in enumerate(longitudes)}
    rows = np.array([lat_index[value] for value in table["latitude_deg"]], dtype=np.int64)
    cols = np.array([lon_index[value] for value in table["longitude_deg"]], dtype=np.int64)

    shape = {"coefficients": (nlat, nlon, 9, 5), "feature_mean": (nlat, nlon, 8),
             "feature_scale": (nlat, nlon, 8), "target_mean": (nlat, nlon, 5),
             "target_scale": (nlat, nlon, 5)}
    artifact = {name: np.full(size, np.nan, dtype=np.float64) for name, size in shape.items()}
    counts = np.zeros((nlat, nlon), dtype=np.int64)
    flat = rows * nlon + cols
    order = np.argsort(flat, kind="stable")
    flat_sorted = flat[order]
    bounds = np.searchsorted(flat_sorted, np.arange(nlat * nlon + 1))
    for cell in range(nlat * nlon):
        selection = order[bounds[cell]:bounds[cell + 1]]
        counts[cell // nlon, cell % nlon] = len(selection)
        if len(selection) < min_rows:
            continue
        model = fit_ridge(features[selection], targets[selection], alpha)
        for name, value in model.items():
            artifact[name][cell // nlon, cell % nlon] = value
    summary = {
        "grid": [int(nlat), int(nlon)],
        "cells_total": int(nlat * nlon),
        "cells_trained": int(np.isfinite(artifact["coefficients"]).all(axis=(2, 3)).sum()),
        "rows_per_cell_min": int(counts[counts > 0].min()) if (counts > 0).any() else 0,
        "rows_per_cell_median": float(np.median(counts[counts > 0])) if (counts > 0).any() else 0.0,
        "trained_leads": sorted(int(value) for value in table["lead_hours"].unique()),
    }
    return {"latitudes": latitudes, "longitudes": longitudes, **artifact}, summary


def pooled_metrics(table: pd.DataFrame, model: dict, alpha: float) -> dict:
    """Refit-free evaluation is impossible per cell, so metrics refit on the
    evaluation subset's own training rows only when the caller splits years;
    here we report the in-sample improvement of the saved cells."""
    features = make_features(table)
    latitudes, longitudes = model["latitudes"], model["longitudes"]
    nlon = len(longitudes)
    lat_rows = np.array([int(np.argmin(np.abs(latitudes - value))) for value in table["latitude_deg"]])
    lon_cols = np.array([int(np.argmin(np.abs(longitudes - value))) for value in table["longitude_deg"]])
    cells = lat_rows * nlon + lon_cols
    raw_rmse, corrected_rmse = {}, {}
    for index, field in enumerate(FIELDS):
        observed = table[f"era5_{field}"].to_numpy(dtype=np.float64)
        forecast = table[f"fuxi_{field}"].to_numpy(dtype=np.float64)
        corrected = forecast.copy()
        for cell in np.unique(cells):
            mask = cells == cell
            i, j = cell // nlon, cell % nlon
            coefficients = model["coefficients"][i, j]
            if not np.isfinite(coefficients).all():
                continue
            cell_model = {
                "coefficients": coefficients,
                "feature_mean": model["feature_mean"][i, j],
                "feature_scale": model["feature_scale"][i, j],
                "target_scale": model["target_scale"][i, j],
                "target_mean": model["target_mean"][i, j],
            }
            corrected[mask] = forecast[mask] + apply_ridge(cell_model, features[mask])[:, index]
        raw_rmse[field] = float(np.sqrt(np.nanmean((forecast - observed) ** 2)))
        corrected_rmse[field] = float(np.sqrt(np.nanmean((corrected - observed) ** 2)))
    return {
        "rmse_fuxi": raw_rmse,
        "rmse_corrected": corrected_rmse,
        "improvement_percent": {
            field: round((1 - corrected_rmse[field] / raw_rmse[field]) * 100, 2)
            if raw_rmse[field] > 0 else None for field in FIELDS
        },
        "rows": int(len(table)),
        "alpha": float(alpha),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--alpha", type=float, default=10.0)
    parser.add_argument("--min-rows", type=int, default=50)
    parser.add_argument("--test-year", type=int, help="report metrics on this init year before the final refit")
    parser.add_argument("--no-refit-all", action="store_true",
                        help="keep the model fitted on training years only")
    args = parser.parse_args()

    table = load_table(args.data)
    print(f"loaded {len(table)} rows, {table['init_time_utc'].nunique()} init dates, "
          f"leads {sorted(int(v) for v in table['lead_hours'].unique())}")
    test_metrics = None
    if args.test_year:
        years = table["init_time_utc"].dt.year
        train_table = table[years != args.test_year]
        test_table = table[years == args.test_year]
        print(f"year split: train {len(train_table)} rows, test {len(test_table)} rows ({args.test_year})")
        if not train_table.empty and not test_table.empty:
            held_out, _ = fit_grid(train_table, args.alpha, args.min_rows)
            test_metrics = pooled_metrics(test_table, held_out, args.alpha)
            print(json.dumps(test_metrics, indent=2, ensure_ascii=False))
        if args.no_refit_all:
            table = train_table

    model, summary = fit_grid(table, args.alpha, args.min_rows)
    full_metrics = pooled_metrics(table, model, args.alpha)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = args.output_dir / "corrector_region_ridge.npz"
    np.savez_compressed(
        artifact_path,
        latitudes=model["latitudes"],
        longitudes=model["longitudes"],
        coefficients=model["coefficients"],
        feature_mean=model["feature_mean"],
        feature_scale=model["feature_scale"],
        target_mean=model["target_mean"],
        target_scale=model["target_scale"],
        feature_names=np.asarray(FEATURE_NAMES),
        target_names=np.asarray(TARGET_NAMES),
        trained_leads=np.asarray(summary["trained_leads"], dtype=np.int64),
    )
    metadata = {
        "purpose": "regional_deployment_refit",
        "trained_at_utc": datetime.now(timezone.utc).isoformat(),
        "data_path": str(args.data),
        "rows": int(len(table)),
        "init_dates": int(table["init_time_utc"].nunique()),
        "init_start_utc": table["init_time_utc"].min().isoformat(),
        "init_end_utc": table["init_time_utc"].max().isoformat(),
        "grid_summary": summary,
        "features": list(FEATURE_NAMES),
        "alpha": args.alpha,
        "min_rows": args.min_rows,
        "test_year_metrics": test_metrics,
        "full_fit_metrics": full_metrics,
        "notes": [
            "Per-cell standardized ridge on the deployment eight features; production "
            "applies the nearest trained cell's coefficients.",
            "Cells below min-rows stay NaN and fall back to raw FuXi.",
            "Field policy (which fields actually receive the correction at inference) "
            "stays in Web/backend/weather_service.CORRECTED_FIELDS.",
        ],
    }
    (args.output_dir / "training_metadata_region.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"saved {artifact_path}")
    print(json.dumps({"grid": summary, "metrics": full_metrics}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

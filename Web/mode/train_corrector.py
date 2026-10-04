"""Train and evaluate a small FuXi point-forecast bias corrector.

This script is deliberately conservative for the current small dataset:

* one CSV row is one supervised sample (one FuXi forecast paired with ERA5);
* the target is the residual ``ERA5 - FuXi``;
* the split is chronological, so newer dates are never used to train an
  older-date test sample;
* the model is a regularized linear ridge model implemented with NumPy, so
  the FuXi ONNX environment does not need scikit-learn or PyTorch.

The default command is a preview. Add ``--train`` to write an experiment
report and a model artifact. This module never trains merely when imported.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


DEFAULT_DATA = Path("D:/低空经济/app/point_data/fuxi_era5_pairs_6h.csv")
DEFAULT_OUTPUT = Path("D:/低空经济/Web/mode/output")

FIELDS = (
    "t2m_K",
    "u10_m_s",
    "v10_m_s",
    "msl_Pa",
    "tp_6h_mm",
)
FEATURE_NAMES = tuple(f"fuxi_{field}" for field in FIELDS) + (
    "lead_hours",
    "doy_sin",
    "doy_cos",
)
TARGET_NAMES = tuple(f"residual_{field}" for field in FIELDS)


def load_pairs(path: Path) -> pd.DataFrame:
    """Load, validate, sort, and retain complete paired rows."""
    if not path.is_file():
        raise FileNotFoundError(path)
    table = pd.read_csv(path)
    required = {"init_time_utc", "valid_time_utc", "lead_hours", "latitude_deg", "longitude_deg"}
    for field in FIELDS:
        required.update((f"fuxi_{field}", f"era5_{field}"))
    missing = sorted(required - set(table.columns))
    if missing:
        raise ValueError(f"CSV is missing required columns: {missing}")

    table["init_time_utc"] = pd.to_datetime(table["init_time_utc"], utc=True, errors="coerce")
    table["valid_time_utc"] = pd.to_datetime(table["valid_time_utc"], utc=True, errors="coerce")
    numeric_columns = ["lead_hours", "latitude_deg", "longitude_deg"] + [
        column
        for field in FIELDS
        for column in (f"fuxi_{field}", f"era5_{field}")
    ]
    feature_columns = sorted(column for column in table.columns if column.startswith("feature_"))
    numeric_columns.extend(feature_columns)
    for column in numeric_columns:
        table[column] = pd.to_numeric(table[column], errors="coerce")
    table = table.sort_values(["valid_time_utc", "init_time_utc", "lead_hours"]).reset_index(drop=True)
    if table[["init_time_utc", "valid_time_utc"]].isna().any().any():
        raise ValueError("CSV contains invalid init_time_utc or valid_time_utc values")
    key_columns = ["init_time_utc", "lead_hours", "latitude_deg", "longitude_deg"]
    if table.duplicated(key_columns).any():
        raise ValueError(f"CSV contains duplicate forecast keys: {key_columns}")

    complete = table[numeric_columns].notna().all(axis=1)
    dropped = int((~complete).sum())
    if dropped:
        print(f"WARNING: dropping {dropped} incomplete rows")
        table = table.loc[complete].reset_index(drop=True)
    if len(table) < 12:
        raise ValueError(f"Only {len(table)} complete rows; need at least 12 for a time split")
    return table


def make_features(table: pd.DataFrame) -> tuple[np.ndarray, list[str]]:
    """Build a tabular feature matrix; shape is (samples, features), not an image tensor."""
    timestamps = table["valid_time_utc"]
    day_fraction = (
        timestamps.dt.dayofyear.to_numpy(dtype=np.float64)
        - 1.0
        + timestamps.dt.hour.to_numpy(dtype=np.float64) / 24.0
    )
    angle = 2.0 * np.pi * day_fraction / 365.25
    extra_names = sorted(column for column in table.columns if column.startswith("feature_"))
    columns = [table[f"fuxi_{field}"].to_numpy(dtype=np.float64) for field in FIELDS]
    columns.extend(
        (
            table["lead_hours"].to_numpy(dtype=np.float64),
            np.sin(angle),
            np.cos(angle),
        )
    )
    columns.extend(table[name].to_numpy(dtype=np.float64) for name in extra_names)
    return np.column_stack(columns), [*FEATURE_NAMES, *extra_names]


def residual_targets(table: pd.DataFrame) -> np.ndarray:
    return np.column_stack(
        [
            table[f"era5_{field}"].to_numpy(dtype=np.float64)
            - table[f"fuxi_{field}"].to_numpy(dtype=np.float64)
            for field in FIELDS
        ]
    )


def standardize_fit(values: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mean = values.mean(axis=0)
    scale = values.std(axis=0)
    scale = np.where(scale > 1e-12, scale, 1.0)
    return (values - mean) / scale, mean, scale


def fit_ridge(
    features: np.ndarray,
    targets: np.ndarray,
    alpha: float,
) -> dict[str, np.ndarray | float]:
    """Fit multi-output ridge regression with an unpenalized intercept."""
    if alpha < 0:
        raise ValueError("alpha must be non-negative")
    x_scaled, feature_mean, feature_scale = standardize_fit(features)
    y_scaled, target_mean, target_scale = standardize_fit(targets)
    design = np.column_stack((np.ones(len(x_scaled)), x_scaled))
    penalty = np.eye(design.shape[1], dtype=np.float64) * alpha
    penalty[0, 0] = 0.0
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
        "alpha": float(alpha),
    }


def predict(model: dict[str, np.ndarray | float], features: np.ndarray) -> np.ndarray:
    feature_mean = np.asarray(model["feature_mean"], dtype=np.float64)
    feature_scale = np.asarray(model["feature_scale"], dtype=np.float64)
    target_mean = np.asarray(model["target_mean"], dtype=np.float64)
    target_scale = np.asarray(model["target_scale"], dtype=np.float64)
    coefficients = np.asarray(model["coefficients"], dtype=np.float64)
    scaled = (features - feature_mean) / feature_scale
    design = np.column_stack((np.ones(len(scaled)), scaled))
    return design @ coefficients * target_scale + target_mean


def corrected_forecast(
    fuxi: np.ndarray,
    residual_prediction: np.ndarray,
) -> np.ndarray:
    forecast = fuxi + residual_prediction
    # Six-hour precipitation cannot be physically negative.
    forecast[:, FIELDS.index("tp_6h_mm")] = np.maximum(
        forecast[:, FIELDS.index("tp_6h_mm")], 0.0
    )
    return forecast


def metrics(actual: np.ndarray, prediction: np.ndarray) -> dict[str, dict[str, float]]:
    output: dict[str, dict[str, float]] = {}
    for index, field in enumerate(FIELDS):
        error = prediction[:, index] - actual[:, index]
        output[field] = {
            "mae": float(np.mean(np.abs(error))),
            "rmse": float(np.sqrt(np.mean(error**2))),
            "bias_prediction_minus_era5": float(np.mean(error)),
        }
    return output


def add_skill(
    report: dict[str, dict[str, float]],
    raw: dict[str, dict[str, float]],
    corrected: dict[str, dict[str, float]],
) -> None:
    for field in FIELDS:
        raw_rmse = raw[field]["rmse"]
        corrected_rmse = corrected[field]["rmse"]
        corrected_mae = corrected[field]["mae"]
        raw_mae = raw[field]["mae"]
        report[field] = {
            **corrected[field],
            "skill_vs_raw_rmse_percent": float(
                100.0 * (raw_rmse - corrected_rmse) / raw_rmse
            )
            if raw_rmse > 0
            else 0.0,
            "skill_vs_raw_mae_percent": float(
                100.0 * (raw_mae - corrected_mae) / raw_mae
            )
            if raw_mae > 0
            else 0.0,
        }


def save_model(path: Path, model: dict[str, np.ndarray | float], feature_names: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(
        path,
        coefficients=np.asarray(model["coefficients"]),
        feature_mean=np.asarray(model["feature_mean"]),
        feature_scale=np.asarray(model["feature_scale"]),
        target_mean=np.asarray(model["target_mean"]),
        target_scale=np.asarray(model["target_scale"]),
        feature_names=np.asarray(feature_names),
        target_names=np.asarray(TARGET_NAMES),
        alpha=np.asarray([model["alpha"]], dtype=np.float64),
    )


def run_training(
    table: pd.DataFrame,
    data_path: Path,
    output_dir: Path,
    test_fraction: float,
    alpha: float,
) -> dict[str, object]:
    if not 0 < test_fraction < 0.5:
        raise ValueError("test_fraction must be between 0 and 0.5")
    features, feature_names = make_features(table)
    fuxi = table[[f"fuxi_{field}" for field in FIELDS]].to_numpy(dtype=np.float64)
    actual = table[[f"era5_{field}" for field in FIELDS]].to_numpy(dtype=np.float64)
    targets = actual - fuxi

    test_count = max(1, int(np.ceil(len(table) * test_fraction)))
    train_count = len(table) - test_count
    if train_count <= features.shape[1] + 2:
        raise ValueError(
            f"Only {train_count} training rows for {features.shape[1]} features; "
            "collect more paired forecasts before training"
        )
    train_slice = slice(0, train_count)
    test_slice = slice(train_count, None)

    train_model = fit_ridge(features[train_slice], targets[train_slice], alpha)
    ridge_residual = predict(train_model, features[test_slice])
    train_mean_residual = targets[train_slice].mean(axis=0)
    constant_residual = np.repeat(train_mean_residual[None, :], test_count, axis=0)

    raw_test = fuxi[test_slice]
    actual_test = actual[test_slice]
    constant_test = corrected_forecast(raw_test.copy(), constant_residual)
    ridge_test = corrected_forecast(raw_test.copy(), ridge_residual)
    raw_report = metrics(actual_test, raw_test)
    constant_report: dict[str, dict[str, float]] = {}
    add_skill(constant_report, raw_report, metrics(actual_test, constant_test))
    ridge_report: dict[str, dict[str, float]] = {}
    add_skill(ridge_report, raw_report, metrics(actual_test, ridge_test))

    # Save a candidate fitted on all currently available rows for later use.
    final_model = fit_ridge(features, targets, alpha)
    output_dir.mkdir(parents=True, exist_ok=True)
    save_model(output_dir / "corrector_ridge.npz", final_model, feature_names)

    prediction_table = table.iloc[train_count:][["valid_time_utc"]].copy()
    prediction_table["valid_time_utc"] = prediction_table["valid_time_utc"].dt.strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    for index, field in enumerate(FIELDS):
        prediction_table[f"era5_{field}"] = actual_test[:, index]
        prediction_table[f"fuxi_{field}"] = raw_test[:, index]
        prediction_table[f"constant_{field}"] = constant_test[:, index]
        prediction_table[f"ridge_{field}"] = ridge_test[:, index]
    prediction_table.to_csv(output_dir / "test_predictions.csv", index=False)

    report = {
        "data_path": str(data_path),
        "rows": len(table),
        "train_rows": train_count,
        "test_rows": test_count,
        "train_end_utc": table.valid_time_utc.iloc[train_count - 1].strftime("%Y-%m-%dT%H:%M:%SZ"),
        "test_start_utc": table.valid_time_utc.iloc[train_count].strftime("%Y-%m-%dT%H:%M:%SZ"),
        "test_end_utc": table.valid_time_utc.iloc[-1].strftime("%Y-%m-%dT%H:%M:%SZ"),
        "features": feature_names,
        "target_definition": "ERA5 - FuXi residual; corrected forecast = FuXi + predicted residual",
        "alpha": alpha,
        "warning": "Exploratory result: the current sample is small and the test period is only the newest 20%.",
        "raw_fuxi": raw_report,
        "constant_bias": constant_report,
        "ridge": ridge_report,
    }
    (output_dir / "metrics.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--test-fraction", type=float, default=0.2)
    parser.add_argument("--alpha", type=float, default=10.0, help="ridge penalty (default: 10)")
    parser.add_argument("--train", action="store_true", help="fit and save the experiment artifacts")
    args = parser.parse_args()

    table = load_pairs(args.data)
    print(f"Rows: {len(table)}")
    print(
        f"Time: {table.valid_time_utc.iloc[0].strftime('%Y-%m-%dT%H:%M:%SZ')} -> "
        f"{table.valid_time_utc.iloc[-1].strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )
    print(f"Features ({len(FEATURE_NAMES)}): {', '.join(FEATURE_NAMES)}")
    if not args.train:
        test_count = max(1, int(np.ceil(len(table) * args.test_fraction)))
        print(f"PREVIEW ONLY: chronological split would use {len(table) - test_count} train rows and {test_count} test rows.")
        print("Add --train to fit the ridge corrector and write output artifacts.")
        return 0

    report = run_training(table, args.data, args.output, args.test_fraction, args.alpha)
    print(f"Saved experiment artifacts to {args.output}")
    for field in FIELDS:
        raw = report["raw_fuxi"][field]["rmse"]
        ridge = report["ridge"][field]["rmse"]
        skill = report["ridge"][field]["skill_vs_raw_rmse_percent"]
        print(f"{field}: raw RMSE={raw:.6g}, ridge RMSE={ridge:.6g}, skill={skill:.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

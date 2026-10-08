"""Evaluate historical FuXi correction on a strictly later calendar year.

Both source tables contribute their common eight predictors. Feature-rich
rows take precedence only after overlapping forecasts and ERA5 labels have
been checked for agreement. Extra feature columns are not zero-filled.
The saved model is fitted exclusively on the requested training years;
test-year observations never participate in fitting or standardization.
Run without --train to inspect the plan without writing experiment files.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

import train_corrector as tc


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA = PROJECT_ROOT / "app/point_data/fuxi_era5_features.csv"
DEFAULT_HISTORY = PROJECT_ROOT / "app/point_data/fuxi_era5_pairs_6h.csv"
KEY_COLUMNS = ["init_time_utc", "lead_hours", "latitude_deg", "longitude_deg"]
VALUE_COLUMNS = [
    f"{source}_{field}" for field in tc.FIELDS for source in ("fuxi", "era5")
]
COMMON_COLUMNS = [*KEY_COLUMNS, "valid_time_utc", *VALUE_COLUMNS]
SEASONS = {
    12: "winter", 1: "winter", 2: "winter",
    3: "spring", 4: "spring", 5: "spring",
    6: "summer", 7: "summer", 8: "summer",
    9: "autumn", 10: "autumn", 11: "autumn",
}


def load_common_table(path: Path) -> pd.DataFrame:
    """Validate common predictors without dropping rows for unused extras."""
    table = pd.read_csv(path, usecols=lambda name: name in COMMON_COLUMNS)
    missing = set(COMMON_COLUMNS) - set(table.columns)
    if missing:
        raise ValueError(f"Missing common columns in {path}: {sorted(missing)}")
    for name in ("init_time_utc", "valid_time_utc"):
        table[name] = pd.to_datetime(table[name], utc=True, errors="raise")
        if table[name].isna().any():
            raise ValueError(f"Missing {name} in {path}")
    numeric = ["lead_hours", "latitude_deg", "longitude_deg", *VALUE_COLUMNS]
    for name in numeric:
        table[name] = pd.to_numeric(table[name], errors="raise")
    if not np.isfinite(table[numeric].to_numpy(dtype=float)).all():
        raise ValueError(f"Non-finite common predictors or labels in {path}")
    lead = table.lead_hours.to_numpy(dtype=float)
    if np.any((lead < 6) | (lead % 6 != 0)):
        raise ValueError(f"Invalid six-hourly forecast leads in {path}")
    if not (
        table.init_time_utc + pd.to_timedelta(table.lead_hours, unit="h")
    ).equals(table.valid_time_utc):
        raise ValueError(f"init_time + lead does not equal valid_time in {path}")
    if table.duplicated(KEY_COLUMNS).any():
        raise ValueError(f"Duplicate forecast keys within {path}")
    return table[COMMON_COLUMNS].copy()


def merge_sources(
    history: pd.DataFrame, features: pd.DataFrame,
) -> tuple[pd.DataFrame, int]:
    """Deduplicate sources, rejecting conflicting records instead of hiding them."""
    overlap = history.merge(features, on=KEY_COLUMNS, suffixes=("_old", "_new"))
    for name in ["valid_time_utc", *VALUE_COLUMNS]:
        if not np.array_equal(
            overlap[f"{name}_old"].to_numpy(),
            overlap[f"{name}_new"].to_numpy(),
        ):
            raise ValueError(f"Conflicting overlapping source values for {name}")
    table = pd.concat([history, features], ignore_index=True)
    table = table.drop_duplicates(KEY_COLUMNS, keep="last")
    table = table.sort_values(["init_time_utc", "lead_hours", "latitude_deg", "longitude_deg"])
    return table.reset_index(drop=True), len(overlap)


def split_years(
    table: pd.DataFrame, train_years: list[int], test_year: int,
) -> tuple[pd.DataFrame, pd.DataFrame, int]:
    """Keep complete init groups out of test and purge later training labels."""
    if not train_years or any(year >= test_year for year in train_years):
        raise ValueError("Every training year must precede the test year")
    test_start = pd.Timestamp(f"{test_year}-01-01", tz="UTC")
    test_end = pd.Timestamp(f"{test_year + 1}-01-01", tz="UTC")
    eligible = table.init_time_utc.dt.year.isin(train_years)
    train_mask = eligible & table.valid_time_utc.lt(test_start)
    test_mask = (
        table.init_time_utc.ge(test_start)
        & table.init_time_utc.lt(test_end)
        & table.valid_time_utc.lt(test_end)
    )
    train = table.loc[train_mask].reset_index(drop=True)
    test = table.loc[test_mask].reset_index(drop=True)
    if train.empty or test.empty:
        raise ValueError("Requested years have no eligible training or test rows")
    if set(train.init_time_utc).intersection(test.init_time_utc):
        raise ValueError("An initialization appears in both training and test")
    if train.valid_time_utc.max() >= test.init_time_utc.min():
        raise ValueError("A training label was not available before test initialization")
    return train, test, int((eligible & ~train_mask).sum())


def profile(table: pd.DataFrame) -> dict[str, object]:
    return {
        "rows": len(table),
        "initializations": table.init_time_utc.nunique(),
        "dates": table.init_time_utc.dt.date.nunique(),
        "init_start_utc": table.init_time_utc.min().isoformat(),
        "init_end_utc": table.init_time_utc.max().isoformat(),
        "valid_start_utc": table.valid_time_utc.min().isoformat(),
        "valid_end_utc": table.valid_time_utc.max().isoformat(),
        "rows_by_init_year": table.init_time_utc.dt.year.value_counts().sort_index().to_dict(),
        "rows_by_init_hour": table.init_time_utc.dt.hour.value_counts().sort_index().to_dict(),
        "rows_by_lead_hours": table.lead_hours.value_counts().sort_index().to_dict(),
    }


def score(actual: np.ndarray, prediction: np.ndarray, raw: dict) -> dict:
    report: dict = {}
    tc.add_skill(report, raw, tc.metrics(actual, prediction))
    return report


def grouped_metrics(
    groups: pd.Series, actual: np.ndarray, raw: np.ndarray,
    corrected: np.ndarray, constant: np.ndarray,
) -> pd.DataFrame:
    """Pool errors per group; do not average monthly skill percentages."""
    records = []
    for group in sorted(groups.unique()):
        selected = groups.eq(group).to_numpy()
        raw_report = tc.metrics(actual[selected], raw[selected])
        ridge_report = score(actual[selected], corrected[selected], raw_report)
        constant_report = score(actual[selected], constant[selected], raw_report)
        for field in tc.FIELDS:
            records.append({
                "period": group, "rows": int(selected.sum()), "field": field,
                "raw_rmse": raw_report[field]["rmse"],
                "ridge_rmse": ridge_report[field]["rmse"],
                "ridge_skill_rmse_percent": ridge_report[field]["skill_vs_raw_rmse_percent"],
                "raw_mae": raw_report[field]["mae"],
                "ridge_mae": ridge_report[field]["mae"],
                "constant_rmse": constant_report[field]["rmse"],
                "constant_skill_rmse_percent": constant_report[field]["skill_vs_raw_rmse_percent"],
            })
    return pd.DataFrame(records)


def evaluate(
    train: pd.DataFrame, test: pd.DataFrame, alpha: float,
) -> tuple[dict, dict, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    x_train, names = tc.make_features(train)
    x_test, test_names = tc.make_features(test)
    if names != list(tc.FEATURE_NAMES) or names != test_names:
        raise ValueError("This experiment requires the common eight predictors")
    if len(train) <= len(names) + 2:
        raise ValueError("Too few training rows for the common predictors")
    model = tc.fit_ridge(x_train, tc.residual_targets(train), alpha)
    raw = test[[f"fuxi_{field}" for field in tc.FIELDS]].to_numpy(dtype=float)
    actual = test[[f"era5_{field}" for field in tc.FIELDS]].to_numpy(dtype=float)
    corrected = tc.corrected_forecast(raw.copy(), tc.predict(model, x_test))
    constant = tc.corrected_forecast(
        raw.copy(), np.broadcast_to(tc.residual_targets(train).mean(axis=0), raw.shape),
    )
    raw_report = tc.metrics(actual, raw)
    scores = {
        "raw_fuxi": raw_report,
        "constant_bias": score(actual, constant, raw_report),
        "ridge": score(actual, corrected, raw_report),
    }
    predictions = test[[*KEY_COLUMNS, "valid_time_utc"]].copy()
    for name in ("init_time_utc", "valid_time_utc"):
        predictions[name] = predictions[name].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    for index, field in enumerate(tc.FIELDS):
        for source, values in (
            ("era5", actual), ("fuxi", raw), ("constant", constant), ("ridge", corrected),
        ):
            predictions[f"{source}_{field}"] = values[:, index]
    months = grouped_metrics(
        test.valid_time_utc.dt.strftime("%Y-%m"), actual, raw, corrected, constant,
    )
    seasons = grouped_metrics(
        test.valid_time_utc.dt.month.map(SEASONS), actual, raw, corrected, constant,
    )
    return model, scores, predictions, months, seasons


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--history-data", type=Path, default=DEFAULT_HISTORY)
    parser.add_argument("--train-years", type=int, nargs="+", default=[2024, 2025])
    parser.add_argument("--test-year", type=int, default=2026)
    parser.add_argument("--alpha", type=float, default=10.0)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--train", action="store_true", help="fit and save the historical-year experiment")
    args = parser.parse_args()
    args.train_years = sorted(set(args.train_years))
    if not np.isfinite(args.alpha) or args.alpha < 0:
        parser.error("alpha must be a finite nonnegative number")
    if any(year >= args.test_year for year in args.train_years):
        parser.error("training years must precede the test year")
    if args.output is None:
        args.output = Path(__file__).resolve().parent / f"output_year_transfer_{args.test_year}"
    history = load_common_table(args.history_data)
    features = load_common_table(args.data)
    merged, overlap = merge_sources(history, features)
    train, test, purged = split_years(merged, args.train_years, args.test_year)
    print(f"Sources: {len(history)} history + {len(features)} feature-table rows")
    print(f"Removed overlapping rows: {overlap}; unique rows: {len(merged)}")
    print(f"Training years {args.train_years}: {len(train)} rows; purged labels: {purged}")
    print(f"Test year {args.test_year}: {len(test)} rows")
    print(f"Latest training label: {train.valid_time_utc.max().isoformat()}")
    print(f"First test initialization: {test.init_time_utc.min().isoformat()}")
    print(f"Features ({len(tc.FEATURE_NAMES)}): {', '.join(tc.FEATURE_NAMES)}")
    hours = sorted(test.init_time_utc.dt.hour.unique().tolist())
    leads = sorted(test.lead_hours.unique().tolist())
    print(f"Test init hours: {hours}; leads: {leads}")
    if not args.train:
        print("PREVIEW ONLY. Add --train to fit and save results.")
        return 0
    model, scores, predictions, months, seasons = evaluate(train, test, args.alpha)
    report = {
        "data_path": str(args.data), "history_data_path": str(args.history_data),
        "source_rows": {"history": len(history), "features": len(features)},
        "duplicate_rows_removed": overlap, "merged_rows": len(merged),
        "train_years": args.train_years, "test_year": args.test_year,
        "train_rows": len(train), "test_rows": len(test), "purged_training_rows": purged,
        "train": profile(train), "test": profile(test),
        "features": list(tc.FEATURE_NAMES), "alpha": args.alpha,
        "model_fit_scope": "training years only; test labels excluded from model and scalers",
        "target_definition": "ERA5 - FuXi residual; corrected forecast = FuXi + predicted residual",
        "notes": [
            "Only shared predictors are used; missing rich features are not imputed.",
            "Only the available part of the test year is evaluated.",
            "Results are retrospective ERA5-reference scores, not station-observation validation.",
            "Training and test initialization hours and leads may have different coverage; see profiles.",
        ],
        **scores,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    tc.save_model(args.output / "corrector_ridge.npz", model, list(tc.FEATURE_NAMES))
    predictions.to_csv(args.output / "test_predictions.csv", index=False)
    months.to_csv(args.output / "monthly_metrics.csv", index=False)
    seasons.to_csv(args.output / "seasonal_metrics.csv", index=False)
    (args.output / "metrics.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    for field in tc.FIELDS:
        raw_rmse = scores["raw_fuxi"][field]["rmse"]
        ridge_rmse = scores["ridge"][field]["rmse"]
        skill = scores["ridge"][field]["skill_vs_raw_rmse_percent"]
        print(f"{field}: raw RMSE={raw_rmse:.6g}, ridge RMSE={ridge_rmse:.6g}, skill={skill:.2f}%")
    print(f"Saved training-only model and test-year artifacts to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

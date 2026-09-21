"""Diagnose seasonal drift in the FuXi point-forecast bias.

The ridge corrector trained on 2026-01..2026-07 gained skill on msl and
tp but lost skill on t2m and u10 in the newest 20% test block (pure
summer).  This script checks whether that failure is a seasonal effect:

* a monthly bias table: mean residual ``ERA5 - FuXi`` per calendar month
  plus the systematic share ``|mean| / RMSE`` of that month;
* a rolling-origin evaluation: for every calendar month with enough
  earlier rows, fit the same constant/ridge correctors on rows strictly
  before that month and score raw vs corrected forecasts inside it,
  once with all earlier rows (expanding) and once with only a recent
  lookback window (recent), so stale off-season data can be compared.

Artifacts are written next to the corrector outputs.  This module never
runs the diagnosis merely when imported.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

import train_corrector as tc


DEFAULT_DATA = Path("D:/低空经济/app/point_data/fuxi_era5_pairs_6h.csv")
DEFAULT_OUTPUT = Path("D:/低空经济/Web/mode/output")


def monthly_bias(table: pd.DataFrame) -> pd.DataFrame:
    """Per-month residual mean, RMSE, and the share of RMSE that is systematic."""
    naive = table["valid_time_utc"].dt.tz_localize(None)
    months = naive.dt.to_period("M")
    records: list[dict[str, float | int | str]] = []
    for period, group in table.groupby(months, sort=True):
        residuals = tc.residual_targets(group)
        record: dict[str, float | int | str] = {"month": str(period), "rows": len(group)}
        for index, field in enumerate(tc.FIELDS):
            column = residuals[:, index]
            mean = float(np.mean(column))
            rmse = float(np.sqrt(np.mean(column**2)))
            record[f"bias_{field}"] = mean
            record[f"rmse_{field}"] = rmse
            record[f"systematic_share_{field}"] = abs(mean) / rmse if rmse > 0 else 0.0
        records.append(record)
    return pd.DataFrame(records)


def rolling_monthly_skill(
    table: pd.DataFrame,
    alpha: float,
    lookback_days: int,
    min_test_rows: int,
) -> pd.DataFrame:
    """Score constant/ridge correctors per calendar month, trained only on earlier rows."""
    naive = table["valid_time_utc"].dt.tz_localize(None)
    months = naive.dt.to_period("M")
    features, _ = tc.make_features(table)
    targets = tc.residual_targets(table)
    fuxi = table[[f"fuxi_{field}" for field in tc.FIELDS]].to_numpy(dtype=np.float64)
    actual = table[[f"era5_{field}" for field in tc.FIELDS]].to_numpy(dtype=np.float64)

    records: list[dict[str, object]] = []
    for period in sorted(months.unique()):
        month_start = period.to_timestamp(how="start")
        test_mask = months.eq(period)
        for mode, train_mask in (
            ("expanding", naive < month_start),
            (
                "recent",
                (naive < month_start)
                & (naive >= month_start - pd.Timedelta(days=lookback_days)),
            ),
        ):
            train_positions = np.flatnonzero(train_mask.to_numpy())
            test_positions = np.flatnonzero(test_mask.to_numpy())
            record: dict[str, object] = {
                "month": str(period),
                "mode": mode,
                "train_rows": len(train_positions),
                "test_rows": len(test_positions),
            }
            records.append(record)
            if len(test_positions) < min_test_rows:
                record["status"] = f"skipped: fewer than {min_test_rows} test rows"
                continue
            if len(train_positions) <= features.shape[1] + 2:
                record["status"] = "skipped: not enough earlier training rows"
                continue
            model = tc.fit_ridge(features[train_positions], targets[train_positions], alpha)
            residual_prediction = tc.predict(model, features[test_positions])
            constant_residual = np.repeat(
                targets[train_positions].mean(axis=0)[None, :],
                len(test_positions),
                axis=0,
            )
            raw = fuxi[test_positions]
            raw_report = tc.metrics(actual[test_positions], raw)
            constant_report: dict[str, dict[str, float]] = {}
            tc.add_skill(
                constant_report,
                raw_report,
                tc.metrics(
                    actual[test_positions],
                    tc.corrected_forecast(raw.copy(), constant_residual),
                ),
            )
            ridge_report: dict[str, dict[str, float]] = {}
            tc.add_skill(
                ridge_report,
                raw_report,
                tc.metrics(
                    actual[test_positions],
                    tc.corrected_forecast(raw.copy(), residual_prediction),
                ),
            )
            record["status"] = "ok"
            for field in tc.FIELDS:
                record[f"raw_rmse_{field}"] = raw_report[field]["rmse"]
                record[f"constant_skill_{field}"] = constant_report[field][
                    "skill_vs_raw_rmse_percent"
                ]
                record[f"ridge_skill_{field}"] = ridge_report[field][
                    "skill_vs_raw_rmse_percent"
                ]
    return pd.DataFrame(records)


def print_bias_table(bias: pd.DataFrame) -> None:
    print("Monthly mean bias (ERA5 - FuXi):")
    print(f"{'month':>7} {'n':>3} " + " ".join(f"{field:>10}" for field in tc.FIELDS))
    for _, row in bias.iterrows():
        cells = " ".join(f"{row[f'bias_{field}']:>10.3f}" for field in tc.FIELDS)
        print(f"{row['month']:>7} {int(row['rows']):>3} {cells}")
    print("\nSystematic share of monthly RMSE (|bias| / RMSE, 0..1):")
    print(f"{'month':>7} {'n':>3} " + " ".join(f"{field:>10}" for field in tc.FIELDS))
    for _, row in bias.iterrows():
        cells = " ".join(f"{row[f'systematic_share_{field}']:>10.2f}" for field in tc.FIELDS)
        print(f"{row['month']:>7} {int(row['rows']):>3} {cells}")


def print_skill_table(rolling: pd.DataFrame, mode: str) -> None:
    part = rolling[rolling["mode"] == mode]
    print(f"\nRolling-origin ridge skill vs raw RMSE (%) — training: {mode}")
    header = (
        f"{'month':>7} {'train':>5} {'test':>4} "
        + " ".join(f"{field:>10}" for field in tc.FIELDS)
    )
    print(header)
    for _, row in part.iterrows():
        if row.get("status") != "ok":
            print(f"{row['month']:>7} {int(row['train_rows']):>5} {int(row['test_rows']):>4}  {row['status']}")
            continue
        cells = " ".join(f"{row[f'ridge_skill_{field}']:>10.2f}" for field in tc.FIELDS)
        print(
            f"{row['month']:>7} {int(row['train_rows']):>5} {int(row['test_rows']):>4} {cells}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--alpha", type=float, default=10.0, help="ridge penalty (default: 10)")
    parser.add_argument(
        "--lookback-days", type=int, default=90, help="training window for 'recent' mode"
    )
    parser.add_argument("--min-test-rows", type=int, default=6)
    args = parser.parse_args()

    table = tc.load_pairs(args.data)
    print(
        f"Rows: {len(table)}, "
        f"{table.valid_time_utc.iloc[0].strftime('%Y-%m-%d')} -> "
        f"{table.valid_time_utc.iloc[-1].strftime('%Y-%m-%d')}"
    )
    bias = monthly_bias(table)
    rolling = rolling_monthly_skill(
        table, args.alpha, args.lookback_days, args.min_test_rows
    )

    args.output.mkdir(parents=True, exist_ok=True)
    bias.to_csv(args.output / "monthly_bias.csv", index=False)
    rolling.to_csv(args.output / "rolling_skill.csv", index=False)
    report = {
        "data_path": str(args.data),
        "rows": len(table),
        "alpha": args.alpha,
        "lookback_days": args.lookback_days,
        "min_test_rows": args.min_test_rows,
        "monthly_bias": bias.to_dict(orient="records"),
        "rolling_skill": rolling.to_dict(orient="records"),
    }
    (args.output / "seasonality_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print_bias_table(bias)
    print_skill_table(rolling, "expanding")
    print_skill_table(rolling, "recent")
    print(f"\nSaved seasonality artifacts to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

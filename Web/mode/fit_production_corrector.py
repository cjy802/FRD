"""Refit the agreed common-feature corrector on all available paired rows.

This is deployment fitting, not another train/test experiment. Overlapping
source keys are verified and deduplicated. The earlier independent year
evaluation remains separate and is not overwritten or re-scored here.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import train_corrector as tc
from evaluate_year_transfer import (
    DEFAULT_DATA, DEFAULT_HISTORY, PROJECT_ROOT, load_common_table, merge_sources, profile,
)


DEFAULT_OUTPUT = PROJECT_ROOT / "Web/mode/output_production"


def fit_all_rows(table, alpha: float = 10.0):
    features, names = tc.make_features(table)
    if names != list(tc.FEATURE_NAMES):
        raise ValueError("Deployment fitting requires the agreed common eight predictors")
    if len(table) <= len(names) + 2:
        raise ValueError("Too few complete paired rows for deployment fitting")
    return tc.fit_ridge(features, tc.residual_targets(table), alpha), names


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--history-data", type=Path, default=DEFAULT_HISTORY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--alpha", type=float, default=10.0)
    parser.add_argument("--train", action="store_true", help="fit all paired rows and save deployment model")
    args = parser.parse_args()
    if not np.isfinite(args.alpha) or args.alpha < 0:
        parser.error("alpha must be finite and nonnegative")
    history = load_common_table(args.history_data)
    features = load_common_table(args.data)
    table, overlap = merge_sources(history, features)
    print(f"Sources: {len(history)} history + {len(features)} feature-table rows")
    print(f"Removed overlapping rows: {overlap}")
    print(f"All training rows: {len(table)}; no held-out rows")
    print(f"Training years: {sorted(table.init_time_utc.dt.year.unique().tolist())}")
    print(f"Latest training label: {table.valid_time_utc.max().isoformat()}")
    print(f"Features ({len(tc.FEATURE_NAMES)}): {', '.join(tc.FEATURE_NAMES)}")
    if not args.train:
        print("PREVIEW ONLY. Add --train to refit and save the production model.")
        return 0
    model, names = fit_all_rows(table, args.alpha)
    args.output.mkdir(parents=True, exist_ok=True)
    model_path = args.output / "corrector_ridge.npz"
    pending_model = args.output / "corrector_ridge.pending.npz"
    tc.save_model(pending_model, model, names)
    pending_model.replace(model_path)
    metadata = {
        "purpose": "deployment_refit", "model_fit_scope": "all deduplicated available paired rows",
        "trained_at_utc": datetime.now(timezone.utc).isoformat(),
        "data_path": str(args.data), "history_data_path": str(args.history_data),
        "source_rows": {"history": len(history), "features": len(features)},
        "duplicate_rows_removed": overlap, "train_rows": len(table), "held_out_rows": 0,
        "training_profile": profile(table), "features": names, "alpha": args.alpha,
        "field_policy": {
            "t2m_K": "ridge", "u10_m_s": "ridge", "v10_m_s": "fuxi",
            "msl_Pa": "fuxi", "tp_6h_mm": "ridge",
        },
        "notes": [
            "All available 2024/2025/2026 rows are used; no new validation split is made.",
            "Rich predictors absent from the history table are not zero-filled or used.",
            "Previous year-transfer validation is separate; this refit has no independent test score.",
            "Prediction still requires a current FuXi raw forecast or fresh global initial field.",
        ],
    }
    pending_metadata = args.output / "training_metadata.pending.json"
    pending_metadata.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    pending_metadata.replace(args.output / "training_metadata.json")
    print(f"Saved production model: {model_path}")
    print("Active policy: correct temperature/U10/precipitation; preserve raw FuXi V10/MSL")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Pair existing monthly backfill inputs without downloading from CDS.

Each selected day uses the already downloaded pressure ZIP and monthly
single-level NetCDF files, runs the existing FuXi +6h point pairing, verifies
that the master CSV gained that day's key, and optionally removes only that
day's pressure archive. The default is a dry run.
"""

from __future__ import annotations

import argparse
import csv
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import date, datetime
from pathlib import Path


PROJECT_ROOT = Path("D:/低空经济")
FUXI_PYTHON = Path("D:/anaconda/envs/fuxi_onnx/python.exe")
MODEL_DIR = Path("D:/DATASET/FuXi_RuntimeLinks/FuXiEC")
DEFAULT_BATCH_ROOT = Path("D:/DATASET/FuXi_ERA5/backfill")
DEFAULT_PAIRS = PROJECT_ROOT / "app/point_data/fuxi_era5_pairs_6h.csv"
PRESSURE_NAME = re.compile(r"era5_pressure_(\d{8})_00_06(?:_12_18)?\.zip$")


def pair_key(day: date) -> tuple[str, int]:
    return (f"{day:%Y-%m-%d}T06:00:00Z", 6)


def existing_keys(path: Path) -> set[tuple[str, int]]:
    if not path.is_file():
        return set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return {
            (row["init_time_utc"], int(row["lead_hours"]))
            for row in reader
        }


def observation_days(path: Path) -> set[date]:
    if not path.is_file():
        return set()
    days: set[date] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            text = row.get("valid_time_utc", "")
            try:
                stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                continue
            if stamp.hour == 12:
                days.add(stamp.date())
    return days


def pressure_files(month_dir: Path) -> dict[date, list[Path]]:
    found: dict[date, list[Path]] = {}
    for path in month_dir.glob("era5_pressure_*.zip"):
        match = PRESSURE_NAME.fullmatch(path.name)
        if match is None or not zipfile.is_zipfile(path):
            continue
        day = datetime.strptime(match.group(1), "%Y%m%d").date()
        found.setdefault(day, []).append(path)
    return found


def month_sources(month_dir: Path, year: int, month: int) -> tuple[Path, Path, Path]:
    stamp = f"{year}{month:02d}"
    paths = (
        month_dir / f"single_surface_{stamp}.nc",
        month_dir / f"single_wind_{stamp}.nc",
        month_dir / f"precip_{stamp}.nc",
    )
    missing = [path for path in paths if not path.is_file()]
    if missing:
        raise FileNotFoundError("Missing monthly source(s): " + ", ".join(map(str, missing)))
    return paths


def run_one_day(
    day: date,
    month_dir: Path,
    sources: tuple[Path, Path, Path],
    observation_csv: Path,
    pairs_csv: Path,
    model_dir: Path,
    fuxi_python: Path,
) -> None:
    command = [
        str(fuxi_python),
        str(PROJECT_ROOT / "app/build_fuxi_pairs.py"),
        "--pressure-dir", str(month_dir),
        "--surface", str(sources[0]),
        "--wind", str(sources[1]),
        "--precip", str(sources[2]),
        "--model-dir", str(model_dir),
        "--observations", str(observation_csv),
        "--output", str(pairs_csv),
        "--date", day.isoformat(),
        "--run", "--max-runs", "1",
    ]
    print("Pairing:", day.isoformat())
    result = subprocess.run(command)
    if result.returncode != 0:
        raise RuntimeError(f"build_fuxi_pairs.py failed for {day}: {result.returncode}")


def cleanup_day(day: date, month_dir: Path, files: list[Path]) -> int:
    removed_bytes = 0
    for path in files:
        if path.exists():
            removed_bytes += path.stat().st_size
            path.unlink()
    # Remove a leftover daily subdirectory from a previous daily-cleanup run,
    # but only after this day's pair has been verified.
    leftover = month_dir / f"pressure_{day:%Y%m%d}"
    if leftover.is_dir():
        for path in leftover.rglob("*"):
            if path.is_file():
                removed_bytes += path.stat().st_size
        shutil.rmtree(leftover)
    return removed_bytes


def process_month(args: argparse.Namespace, year: int, month: int) -> None:
    month_dir = args.batch_root / f"{year}-{month:02d}"
    if not month_dir.is_dir():
        print(f"SKIP {year}-{month:02d}: batch directory missing: {month_dir}")
        return
    sources = month_sources(month_dir, year, month)
    files_by_day = pressure_files(month_dir)
    obs_days = observation_days(args.observations)
    keys = existing_keys(args.pairs_csv)
    candidates = sorted(files_by_day)
    print(
        f"\n=== {year}-{month:02d}: existing pressure days={len(candidates)}, "
        f"observed 12UTC days={sum(day in obs_days for day in candidates)} ==="
    )
    if not candidates:
        print("No valid pressure ZIPs found; no download will be attempted.")
        return
    if not args.execute:
        for day in candidates:
            status = "paired" if pair_key(day) in keys else (
                "blocked: missing 12UTC observation" if day not in obs_days else "ready"
            )
            print(f"  {day.isoformat()}: {status}")
        return

    if not args.fuxi_python.is_file():
        raise FileNotFoundError(args.fuxi_python)
    if not (args.model_dir / "short.onnx").is_file():
        raise FileNotFoundError(args.model_dir / "short.onnx")

    for day in candidates:
        if pair_key(day) in keys:
            continue
        if day not in obs_days:
            print(f"BLOCKED {day.isoformat()}: no 12UTC observation; preserving its ZIP")
            continue
        run_one_day(
            day, month_dir, sources, args.observations, args.pairs_csv,
            args.model_dir, args.fuxi_python,
        )
        keys = existing_keys(args.pairs_csv)
        if pair_key(day) not in keys:
            raise RuntimeError(f"Pair key was not appended for {day}; preserving its ZIP")
        if args.cleanup_pressure:
            freed = cleanup_day(day, month_dir, files_by_day[day])
            print(f"Verified and removed {day.isoformat()} pressure data: {freed / 1e9:.2f} GB")

    keys = existing_keys(args.pairs_csv)
    remaining = [day for day in candidates if pair_key(day) not in keys]
    if args.cleanup_month_inputs and not remaining:
        removed = 0
        for path in sources:
            if path.exists():
                removed += path.stat().st_size
                path.unlink()
        previous_day = date.fromordinal(date(year, month, 1).toordinal() - 1)
        for name in (
            f"precip_prev_{previous_day:%Y%m%d}.nc",
            f"precip_month_{year}{month:02d}.nc",
        ):
            path = month_dir / name
            if path.exists():
                removed += path.stat().st_size
                path.unlink()
        print(f"All existing pairs complete; removed monthly sources: {removed / 1e9:.2f} GB")
    elif args.cleanup_month_inputs and remaining:
        print(f"Keeping monthly sources; {len(remaining)} day(s) remain unpaired")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--months", type=int, nargs="+", default=list(range(1, 13)))
    parser.add_argument("--batch-root", type=Path, default=DEFAULT_BATCH_ROOT)
    parser.add_argument("--pairs-csv", type=Path, default=DEFAULT_PAIRS)
    parser.add_argument("--observations", type=Path, help="point ERA5 CSV; defaults to the requested year")
    parser.add_argument("--model-dir", type=Path, default=MODEL_DIR)
    parser.add_argument("--fuxi-python", type=Path, default=FUXI_PYTHON)
    parser.add_argument("--execute", action="store_true", help="run GPU pairing; default is preview")
    parser.add_argument("--cleanup-pressure", action="store_true", help="delete a day's pressure ZIP after verified pairing")
    parser.add_argument("--cleanup-month-inputs", action="store_true", help="delete monthly NetCDF sources only after all candidate days are paired")
    args = parser.parse_args()
    if not args.execute and (args.cleanup_pressure or args.cleanup_month_inputs):
        parser.error("cleanup options require --execute")
    if not args.observations:
        stem = f"era5_surface_point_31p5N_119p5E_{args.year}.csv"
        args.observations = PROJECT_ROOT / "app/point_data" / stem
    invalid = [month for month in args.months if month < 1 or month > 12]
    if invalid:
        parser.error(f"invalid month(s): {invalid}")
    for month in sorted(set(args.months)):
        try:
            process_month(args, args.year, month)
        except Exception as exc:  # noqa: BLE001 - stop before deleting more data
            print(f"FAILED {args.year}-{month:02d}: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

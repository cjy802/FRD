"""Download only the global pressure-level frames needed for FuXi-1.0.

Default behaviour is a dry run. Each selected date creates one CDS request for
00:00 and 06:00 UTC, suitable for a FuXi initialization at 06:00 UTC.
Surface-level data (including precipitation) must be requested separately.
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from datetime import date, timedelta
from pathlib import Path


DATASET = "reanalysis-era5-pressure-levels"
VARIABLES = [
    "geopotential",
    "relative_humidity",
    "temperature",
    "u_component_of_wind",
    "v_component_of_wind",
]
PRESSURE_LEVELS = [
    "50", "100", "150", "200", "250", "300", "400",
    "500", "600", "700", "850", "925", "1000",
]
TIMES = ["00:00", "06:00"]
DEFAULT_OUTPUT = Path("D:/DATASET/FuXi_ERA5/pressure")


def parse_dates(args: argparse.Namespace) -> list[date]:
    if args.date and (args.start or args.end):
        raise ValueError("--date cannot be combined with --start/--end")
    if bool(args.start) != bool(args.end):
        raise ValueError("--start and --end must be supplied together")
    if args.every_days < 1:
        raise ValueError("--every-days must be at least 1")

    if args.date:
        dates = sorted({date.fromisoformat(text) for text in args.date})
    elif args.start:
        first = date.fromisoformat(args.start)
        last = date.fromisoformat(args.end)
        if last < first:
            raise ValueError("--end must not precede --start")
        dates = []
        current = first
        while current <= last:
            dates.append(current)
            current += timedelta(days=args.every_days)
    else:
        dates = [date(2026, 1, 15)]
    return dates


def build_request(day: date) -> dict[str, list[str] | str]:
    return {
        "product_type": ["reanalysis"],
        "variable": VARIABLES,
        "year": [f"{day.year:04d}"],
        "month": [f"{day.month:02d}"],
        "day": [f"{day.day:02d}"],
        "time": TIMES,
        "pressure_level": PRESSURE_LEVELS,
        "data_format": "netcdf",
        "download_format": "zip",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="FuXi-1.0 ERA5 global pressure levels, 00/06 UTC; dry-run by default"
    )
    parser.add_argument("--date", action="append", help="UTC init date YYYY-MM-DD; repeatable")
    parser.add_argument("--start", help="first UTC init date YYYY-MM-DD")
    parser.add_argument("--end", help="last UTC init date YYYY-MM-DD (inclusive)")
    parser.add_argument("--every-days", type=int, default=7, help="date interval with --start/--end (default: 7)")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--download", action="store_true", help="actually submit CDS requests")
    parser.add_argument(
        "--max-downloads", type=int, default=1,
        help="safety cap on number of requests in one run (default: 1)",
    )
    args = parser.parse_args()

    try:
        dates = parse_dates(args)
    except ValueError as exc:
        parser.error(str(exc))
    if not dates:
        parser.error("no dates selected")

    # 5 variables x 13 pressure levels x 2 times = 130 fields per date.
    print(f"Selected {len(dates)} date(s), {len(VARIABLES) * len(PRESSURE_LEVELS) * len(TIMES)} fields/date")
    print("Each date uses global 0.25-degree ERA5 data; no region or resolution reduction.")
    print("This script does NOT request FuXi's five surface fields or hourly precipitation.")
    print(f"Output: {args.output_dir}")

    for day in dates:
        target = args.output_dir / f"era5_pressure_{day:%Y%m%d}_00_06.zip"
        print(f"\n{day.isoformat()} -> {target}")
        if not args.download:
            print(json.dumps(build_request(day), ensure_ascii=False, indent=2))

    if not args.download:
        print("\nDRY RUN: no CDS requests were submitted. Add --download to submit.")
        return 0

    if len(dates) > args.max_downloads:
        parser.error(
            f"{len(dates)} requests exceed --max-downloads {args.max_downloads}; "
            "raise the cap explicitly only after checking storage and network cost"
        )

    try:
        import cdsapi
    except ImportError:
        print('Install CDS API in this environment: python -m pip install "cdsapi>=0.7.7"', file=sys.stderr)
        return 2

    args.output_dir.mkdir(parents=True, exist_ok=True)
    client = cdsapi.Client()
    for day in dates:
        target = args.output_dir / f"era5_pressure_{day:%Y%m%d}_00_06.zip"
        if target.exists():
            if zipfile.is_zipfile(target):
                print(f"Skipping existing ZIP: {target}")
                continue
            raise RuntimeError(f"Existing file is not a valid ZIP; inspect it manually: {target}")
        print(f"Submitting {day.isoformat()} to CDS...")
        client.retrieve(DATASET, build_request(day), str(target))
        if not target.is_file() or not zipfile.is_zipfile(target):
            raise RuntimeError(f"CDS output is missing or not a ZIP: {target}")
        print(f"Saved {target} ({target.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

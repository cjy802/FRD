"""Download only the global pressure-level frames needed for FuXi-1.0.

Default behaviour is a dry run. Each selected date creates one CDS request for
the requested snapshots (00:00 and 06:00 UTC by default). Use ``--times`` to
request another set, such as all four six-hourly snapshots.
Surface-level data (including precipitation) must be requested separately.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path
import re
from typing import Callable


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
DEFAULT_TIMES = ["00:00", "06:00"]
FOUR_TIMES = ["00:00", "06:00", "12:00", "18:00"]
# Backward-compatible alias for callers that imported the old constant.
TIMES = DEFAULT_TIMES
DEFAULT_OUTPUT = Path("D:/DATASET/FuXi_ERA5/pressure")


class InvalidDownloadError(RuntimeError):
    """The CDS request returned without producing a valid ZIP archive."""


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


def normalize_times(values: list[str] | None) -> list[str]:
    """Validate and sort requested hourly snapshots."""
    selected = list(values or DEFAULT_TIMES)
    if not selected:
        raise ValueError("at least one pressure time is required")
    parsed: list[tuple[int, str]] = []
    for text in selected:
        if re.fullmatch(r"\d{2}:00", text) is None:
            raise ValueError(f"pressure times must use HH:00 format: {text}")
        hour = int(text[:2])
        if not 0 <= hour <= 23:
            raise ValueError(f"pressure hour is outside 00..23: {text}")
        parsed.append((hour, text))
    hours = [hour for hour, _ in parsed]
    if len(hours) != len(set(hours)):
        raise ValueError("pressure times must not contain duplicates")
    return [text for _, text in sorted(parsed)]


def time_tag(times: list[str]) -> str:
    return "_".join(text[:2] for text in normalize_times(times))


def build_request(
    day: date,
    times: list[str] | None = None,
) -> dict[str, list[str] | str]:
    selected_times = normalize_times(times)
    return {
        "product_type": ["reanalysis"],
        "variable": VARIABLES,
        "year": [f"{day.year:04d}"],
        "month": [f"{day.month:02d}"],
        "day": [f"{day.day:02d}"],
        "time": selected_times,
        "pressure_level": PRESSURE_LEVELS,
        "data_format": "netcdf",
        "download_format": "zip",
    }


def target_path(
    output_dir: Path,
    day: date,
    times: list[str] | None = None,
) -> Path:
    return output_dir / f"era5_pressure_{day:%Y%m%d}_{time_tag(times or DEFAULT_TIMES)}.zip"


def download_with_retries(
    day: date,
    target: Path,
    client_factory: Callable[[], object],
    request_error: type[BaseException],
    attempts: int,
    retry_delay_seconds: float,
    times: list[str] | None = None,
) -> None:
    """Download one day atomically, retrying transient request failures."""
    partial = target.with_name(f"{target.name}.part")
    transient_errors = (request_error, InvalidDownloadError)

    for attempt in range(1, attempts + 1):
        if partial.exists():
            partial.unlink()
        try:
            client = client_factory()
            client.retrieve(DATASET, build_request(day, times), str(partial))
            if not partial.is_file() or not zipfile.is_zipfile(partial):
                raise InvalidDownloadError(f"CDS output is missing or not a ZIP: {partial}")
            partial.replace(target)
            return
        except transient_errors as exc:
            if partial.exists():
                partial.unlink()
            if attempt == attempts:
                raise
            delay = min(retry_delay_seconds * (2 ** (attempt - 1)), 300.0)
            print(
                f"Transient failure for {day.isoformat()} on attempt {attempt}/{attempts}: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            print(f"Retrying this date in {delay:g} seconds...", file=sys.stderr)
            time.sleep(delay)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="FuXi-1.0 ERA5 global pressure levels, 00/06 UTC; dry-run by default"
    )
    parser.add_argument("--date", action="append", help="UTC init date YYYY-MM-DD; repeatable")
    parser.add_argument("--start", help="first UTC init date YYYY-MM-DD")
    parser.add_argument("--end", help="last UTC init date YYYY-MM-DD (inclusive)")
    parser.add_argument("--every-days", type=int, default=7, help="date interval with --start/--end (default: 7)")
    parser.add_argument(
        "--times", nargs="+", help="pressure snapshots such as 00:00 06:00 12:00 18:00; default: 00:00 06:00"
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--download", action="store_true", help="actually submit CDS requests")
    parser.add_argument(
        "--max-downloads", type=int, default=1,
        help="safety cap on number of requests in one run (default: 1)",
    )
    parser.add_argument(
        "--retry-attempts", type=int, default=5,
        help="total attempts for a date after transient network/download errors (default: 5)",
    )
    parser.add_argument(
        "--retry-delay-seconds", type=float, default=30.0,
        help="initial retry delay; doubles up to 300 seconds (default: 30)",
    )
    args = parser.parse_args()

    try:
        dates = parse_dates(args)
        times = normalize_times(args.times)
    except ValueError as exc:
        parser.error(str(exc))
    if not dates:
        parser.error("no dates selected")
    if args.max_downloads < 1:
        parser.error("--max-downloads must be at least 1")
    if args.retry_attempts < 1:
        parser.error("--retry-attempts must be at least 1")
    if args.retry_delay_seconds < 0:
        parser.error("--retry-delay-seconds must not be negative")

    existing: list[tuple[date, Path]] = []
    pending: list[tuple[date, Path]] = []
    for day in dates:
        target = target_path(args.output_dir, day, times)
        if not target.exists():
            pending.append((day, target))
        elif zipfile.is_zipfile(target):
            existing.append((day, target))
        else:
            parser.error(f"existing file is not a valid ZIP; inspect it manually: {target}")

    # 5 variables x 13 pressure levels x 2 times = 130 fields per date.
    print(f"Selected {len(dates)} date(s), {len(VARIABLES) * len(PRESSURE_LEVELS) * len(times)} fields/date")
    print(f"Pressure snapshots: {', '.join(times)}")
    print("Each date uses global 0.25-degree ERA5 data; no region or resolution reduction.")
    print("This script does NOT request FuXi's five surface fields or hourly precipitation.")
    print(f"Output: {args.output_dir}")

    for day, target in existing:
        print(f"\n{day.isoformat()} -> {target}")
        print("Existing valid ZIP; will skip.")
    for day, target in pending:
        print(f"\n{day.isoformat()} -> {target}")
        if not args.download:
            print(json.dumps(build_request(day, times), ensure_ascii=False, indent=2))

    if not args.download:
        print("\nDRY RUN: no CDS requests were submitted. Add --download to submit.")
        return 0

    if len(pending) > args.max_downloads:
        parser.error(
            f"{len(pending)} pending requests exceed --max-downloads {args.max_downloads}; "
            "raise the cap explicitly only after checking storage and network cost"
        )

    try:
        import cdsapi
        import requests
    except ImportError:
        print('Install CDS API in this environment: python -m pip install "cdsapi>=0.7.7"', file=sys.stderr)
        return 2

    args.output_dir.mkdir(parents=True, exist_ok=True)
    failures: list[tuple[date, BaseException]] = []
    for day, target in pending:
        print(f"Submitting {day.isoformat()} to CDS...")
        try:
            download_with_retries(
                day=day,
                target=target,
                client_factory=cdsapi.Client,
                request_error=requests.exceptions.RequestException,
                attempts=args.retry_attempts,
                retry_delay_seconds=args.retry_delay_seconds,
                times=times,
            )
        except (requests.exceptions.RequestException, InvalidDownloadError) as exc:
            failures.append((day, exc))
            print(
                f"FAILED {day.isoformat()} after {args.retry_attempts} attempts; "
                "continuing with the next date.",
                file=sys.stderr,
            )
            continue
        print(f"Saved {target} ({target.stat().st_size:,} bytes)")

    if failures:
        print("\nSome dates failed and remain safe to retry:", file=sys.stderr)
        for day, exc in failures:
            print(f"  {day.isoformat()}: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("Re-run the same command; valid ZIP files will be skipped.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

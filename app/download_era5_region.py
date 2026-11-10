"""Download ERA5 single-level truth for the live service region box.

The whole 2024-01..2026-09 range needs only FIVE CDS requests, because CDS
combines year/month/day lists as a cross product and the region box
(116-122.5E / 30-35.5N, the same 23x27 grid the live FuXi service crops,
~621 points) is tiny: two instantaneous-variable requests (2024-2025 full
years, then 2026 January-September), two hourly-precipitation requests for
the same spans, and one small leading-evening precipitation request so the
six-hour totals at range start do not bridge a gap. Each delivered NetCDF is
a few tens of MB. Submitting many small per-month requests instead trips the
CDS per-dataset queue cap ("Maximum number queued requests ... reached"),
which is why this script talks to CDS as few times as possible.

Finished months become ``era5_surface_box_YYYYMM.csv`` (~10 MB) holding
``valid_time_utc``, ``latitude_deg``, ``longitude_deg`` and the five truth
channels including ``tp_6h_mm`` at the four six-hourly boundary hours. Join
with ``pair_gfs_region.py``. CDS needs the VPN up; the default is a dry run,
add --execute to submit. Re-running skips finished months and valid ZIPs.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

import netCDF4 as nc
import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "FuXi/DATA/era5_region"
DATASET = "reanalysis-era5-single-levels"
AREA = [35.5, 116.0, 30.0, 122.5]  # north, west, south, east
INSTANT_VARIABLES = ["2m_temperature", "mean_sea_level_pressure",
                     "10m_u_component_of_wind", "10m_v_component_of_wind"]
INSTANT_TIMES = ["00:00", "06:00", "12:00", "18:00"]
HOURLY_TIMES = [f"{hour:02d}:00" for hour in range(24)]
EVENING_TIMES = [f"{hour:02d}:00" for hour in range(18, 24)]
TRUTH_COLUMNS = (
    "valid_time_utc", "latitude_deg", "longitude_deg",
    "t2m_K", "msl_Pa", "u10_m_s", "v10_m_s", "tp_6h_mm",
)
INSTANT_NAMES = ("t2m", "msl", "u10", "v10")


def month_list(start: str, end: str) -> list[tuple[int, int]]:
    first = datetime.strptime(start, "%Y-%m")
    last = datetime.strptime(end, "%Y-%m")
    if last < first:
        raise ValueError("--end must not precede --start")
    months = []
    current = first
    while current <= last:
        months.append((current.year, current.month))
        current = (current.replace(day=1) + timedelta(days=32)).replace(day=1)
    return months


def build_jobs(months: list[tuple[int, int]]) -> list[tuple[str, dict]]:
    """Five exact-span requests (cross-product-safe) for the whole range."""
    years = {year for year, _ in months}
    spans: list[tuple[str, tuple[int, ...], tuple[int, ...]]] = []
    for span_years in ([2024, 2025], [2026]):
        covered = [month for year, month in months if year in span_years]
        if covered:
            spans.append(("x", tuple(span_years), tuple(sorted(set(covered)))))
    jobs: list[tuple[str, dict]] = []
    for _, span_years, span_months in spans:
        months_list = [f"{month:02d}" for month in span_months]
        jobs.append((f"instant_{'_'.join(str(y) for y in span_years)}", {
            "product_type": ["reanalysis"], "variable": INSTANT_VARIABLES,
            "year": [f"{year:04d}" for year in span_years],
            "month": months_list, "day": [f"{day:02d}" for day in range(1, 32)],
            "time": INSTANT_TIMES, "area": AREA,
            "data_format": "netcdf", "download_format": "zip",
        }))
    for _, span_years, span_months in spans:
        months_list = [f"{month:02d}" for month in span_months]
        jobs.append((f"precip_{'_'.join(str(y) for y in span_years)}", {
            "product_type": ["reanalysis"], "variable": ["total_precipitation"],
            "year": [f"{year:04d}" for year in span_years],
            "month": months_list, "day": [f"{day:02d}" for day in range(1, 32)],
            "time": HOURLY_TIMES, "area": AREA,
            "data_format": "netcdf", "download_format": "zip",
        }))
    first_year, first_month = months[0]
    if first_month == 1:
        lead_year, lead_month, lead_label = first_year - 1, 12, f"{first_year - 1}12"
    else:
        lead_year, lead_month, lead_label = first_year, first_month - 1, f"{first_year}{first_month - 1:02d}"
    jobs.append((f"precip_leadin_{lead_label}", {
        "product_type": ["reanalysis"], "variable": ["total_precipitation"],
        "year": [f"{lead_year:04d}"], "month": [f"{lead_month:02d}"],
        "day": ["31"],
        "time": EVENING_TIMES, "area": AREA,
        "data_format": "netcdf", "download_format": "zip",
    }))
    # The lead-in must be the LAST day of the previous month, not day 31.
    following = (date(months[0][0], months[0][1], 1) - timedelta(days=1))
    jobs[-1][1]["day"] = [f"{following.day:02d}"]
    return jobs


def valid_zip(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0 and zipfile.is_zipfile(path)


class CdsWorker:
    """Thread-local cdsapi clients; one CDS request per submit call."""

    def __init__(self, attempts: int):
        self.attempts = attempts
        self.local = threading.local()

    def client(self):
        if not hasattr(self.local, "client"):
            import cdsapi

            self.local.client = cdsapi.Client(quiet=True, progress=False)
        return self.local.client

    def submit(self, name: str, payload: dict, target: Path) -> Path:
        partial = target.with_suffix(".part")
        # CDS enforces a small per-dataset queue cap and rejects extra
        # submissions ("request data sequentially"); rejections are loud but
        # transient, so retry patiently with long backoff.
        delays = (300, 600, 900, 900, 1200, 1200, 1800, 1800)
        for attempt in range(1, self.attempts + 1):
            try:
                if partial.exists():
                    partial.unlink()
                self.client().retrieve(DATASET, payload, str(partial))
                if not valid_zip(partial):
                    raise RuntimeError("CDS output is missing or not a ZIP")
                partial.replace(target)
                return target
            except Exception as error:  # noqa: BLE001 - retried, then reported
                if partial.exists():
                    try:
                        partial.unlink()
                    except OSError:
                        pass
                if attempt == self.attempts:
                    raise
                delay = delays[min(attempt - 1, len(delays) - 1)]
                print(f"  {name} attempt {attempt} rejected/failed "
                      f"({type(error).__name__}); retry in {delay}s", flush=True)
                time.sleep(delay)


def extract_zip(zip_path: Path, work_dir: Path) -> Path:
    with zipfile.ZipFile(zip_path) as archive:
        names = [name for name in archive.namelist() if name.endswith(".nc")]
        if len(names) != 1:
            raise RuntimeError(f"expected one NetCDF inside {zip_path.name}, got {names}")
        archive.extract(names[0], work_dir)
        return work_dir / names[0]


def read_grid(dataset: nc.Dataset, names: tuple[str, ...]) -> tuple[pd.DatetimeIndex, np.ndarray, np.ndarray, dict]:
    coordinate = dataset.variables["valid_time"]
    if not str(coordinate.units).startswith("seconds since 1970-01-01"):
        raise ValueError(f"unexpected time units: {coordinate.units}")
    stamps = pd.to_datetime(np.asarray(coordinate[:], dtype=np.int64), unit="s", utc=True)
    if stamps.has_duplicates or not stamps.is_monotonic_increasing:
        raise ValueError("valid_time is duplicated or not increasing")
    latitudes = np.asarray(dataset.variables["latitude"][:], dtype=np.float64)
    longitudes = np.asarray(dataset.variables["longitude"][:], dtype=np.float64)
    values = {
        name: np.asarray(np.ma.filled(dataset.variables[name][:], np.nan), dtype=np.float64)
        for name in names
    }
    for name, grid in values.items():
        if grid.shape != (len(stamps), len(latitudes), len(longitudes)):
            raise ValueError(f"{name} shape {grid.shape} does not match coordinates")
    return stamps, latitudes, longitudes, values


def merge_groups(nc_paths: list[Path], names: tuple[str, ...]) -> tuple[pd.DatetimeIndex, np.ndarray, np.ndarray, dict]:
    """Concatenate same-shape NetCDF groups along time, dropping duplicates."""
    pieces: list[tuple[pd.DatetimeIndex, np.ndarray, np.ndarray, dict]] = []
    for path in nc_paths:
        with nc.Dataset(str(path)) as dataset:
            pieces.append(read_grid(dataset, names))
    latitudes, longitudes = pieces[0][1], pieces[0][2]
    for stamps, lats, lons, _ in pieces[1:]:
        if not (np.allclose(lats, latitudes) and np.allclose(lons, longitudes)):
            raise ValueError("NetCDF groups do not share one grid")
    order = np.argsort(np.concatenate([piece[0].values for piece in pieces]))
    stamps = pd.DatetimeIndex(np.concatenate([piece[0].values for piece in pieces]))[order]
    values = {
        name: np.concatenate([piece[3][name] for piece in pieces], axis=0)[order]
        for name in names
    }
    if stamps.has_duplicates:
        keep = ~stamps.duplicated(keep="first")
        stamps, values = stamps[keep], {name: grid[keep] for name, grid in values.items()}
    return stamps, latitudes, longitudes, values


def extract_months(months: list[tuple[int, int]], instant_ncs: list[Path], precip_ncs: list[Path],
                   output_dir: Path) -> int:
    stamps, latitudes, longitudes, instant = merge_groups(instant_ncs, INSTANT_NAMES)
    precip_stamps, _, _, precip = merge_groups(precip_ncs, ("tp",))
    nlon = len(longitudes)

    hourly = pd.DataFrame({"tp": precip["tp"].reshape(len(precip_stamps), -1)}, index=precip_stamps)
    instants = pd.DataFrame({name: instant[name].reshape(len(stamps), -1) for name in INSTANT_NAMES}, index=stamps)
    combined = instants.join(hourly, how="outer").sort_index()
    consecutive = combined.index.to_series().diff().eq(pd.Timedelta(hours=1))
    six_consecutive = consecutive.rolling(5, min_periods=5).sum().eq(5)
    tp_6h = combined["tp"].rolling(6, min_periods=6).sum().where(six_consecutive) * 1000.0

    lat_round = np.round(latitudes, 3)
    lon_round = np.round(longitudes, 3)
    arrays = {name: np.asarray(combined[name], dtype=np.float64) for name in INSTANT_NAMES}
    tp_boundary = np.asarray(tp_6h, dtype=np.float64)
    written = 0
    for year, month in months:
        month_start = pd.Timestamp(date(year, month, 1), tz="UTC")
        following = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        month_end = pd.Timestamp(following, tz="UTC") - pd.Timedelta(hours=1)
        rows = []
        for time_index, stamp in enumerate(combined.index):
            if not (month_start <= stamp <= month_end) or stamp.hour not in (0, 6, 12, 18):
                continue
            for lat_index, latitude in enumerate(lat_round):
                for lon_index, longitude in enumerate(lon_round):
                    offset = lat_index * nlon + lon_index
                    rows.append((
                        stamp.strftime("%Y-%m-%dT%H:%M:%SZ"), latitude, longitude,
                        arrays["t2m"][time_index, offset], arrays["msl"][time_index, offset],
                        arrays["u10"][time_index, offset], arrays["v10"][time_index, offset],
                        tp_boundary[time_index, offset],
                    ))
        if not rows:
            print(f"{year}-{month:02d}: no boundary frames found", file=sys.stderr, flush=True)
            continue
        frame = pd.DataFrame(rows, columns=TRUTH_COLUMNS)
        if not frame[TRUTH_COLUMNS[3:]].notna().all().all():
            print(f"{year}-{month:02d}: contains missing truth values", file=sys.stderr, flush=True)
        target = output_dir / f"era5_surface_box_{year}{month:02d}.csv"
        frame.to_csv(target, index=False, encoding="utf-8-sig", float_format="%.9g")
        written += 1
        print(f"extracted {year}-{month:02d}: {len(frame)} rows", flush=True)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="first month YYYY-MM")
    parser.add_argument("--end", required=True, help="last month YYYY-MM (inclusive)")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--workers", type=int, default=1,
                        help="concurrent CDS requests (default 1; the dataset queue cap "
                             "rejects anything but sequential requests)")
    parser.add_argument("--execute", action="store_true", help="submit CDS requests")
    parser.add_argument("--retry-attempts", type=int, default=8)
    args = parser.parse_args()

    try:
        months = month_list(args.start, args.end)
    except ValueError as error:
        parser.error(str(error))

    zip_dir = args.output_dir / "zips"
    jobs = build_jobs(months)
    pending_months = [
        (year, month) for year, month in months
        if not (args.output_dir / f"era5_surface_box_{year}{month:02d}.csv").is_file()
    ]
    download_jobs = [(name, payload, zip_dir / f"{name}.zip") for name, payload in jobs
                     if not valid_zip(zip_dir / f"{name}.zip")]
    print(f"{len(months)} month(s) in range, {len(pending_months)} pending, "
          f"{len(download_jobs)} CDS request(s) to submit.", flush=True)
    if not pending_months:
        return 0
    if not args.execute:
        print("DRY RUN: no requests submitted. Add --execute (VPN required).")
        return 0

    try:
        import cdsapi  # noqa: F401
    except ImportError:
        print('python -m pip install "cdsapi>=0.7.7"', file=sys.stderr)
        return 2

    worker = CdsWorker(args.retry_attempts)
    failures: list[str] = []
    if download_jobs:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(worker.submit, name, payload, target): name
                for name, payload, target in download_jobs
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    future.result()
                    print(f"downloaded {name}", flush=True)
                except Exception as error:  # noqa: BLE001
                    failures.append(name)
                    print(f"FAILED {name}: {type(error).__name__}: {str(error)[:200]}",
                          file=sys.stderr, flush=True)

    written = 0
    if not failures:
        try:
            with tempfile.TemporaryDirectory(prefix="era5_box_") as work:
                instant_ncs = [extract_zip(zip_dir / f"{name}.zip", Path(work)) for name, _ in jobs
                               if name.startswith("instant_")]
                precip_ncs = [extract_zip(zip_dir / f"{name}.zip", Path(work)) for name, _ in jobs
                              if name.startswith("precip_")]
                written = extract_months(pending_months, instant_ncs, precip_ncs, args.output_dir)
        except Exception as error:  # noqa: BLE001
            print(f"EXTRACT FAILED: {type(error).__name__}: {str(error)[:200]}", file=sys.stderr, flush=True)
            failures.append("extract")

    print(f"Done: {written} month(s) extracted this run; {len(failures)} failure(s).", flush=True)
    if failures:
        print("Failed: " + ", ".join(sorted(set(failures))), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

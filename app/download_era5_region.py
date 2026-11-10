"""Download ERA5 single-level truth for the live service region box.

One calendar month needs four CDS requests (surface instants, wind instants,
hourly precipitation, preceding-evening precipitation) restricted to
``AREA`` = 116-122.5E / 30-35.5N — the same 23x27 grid the live FuXi service
crops, ~621 points. CDS queues a single request for up to an hour or more,
so instead of one month at a time all missing requests are submitted through
a small thread pool (each thread owns its ``cdsapi.Client``) and months are
extracted as soon as their four ZIPs are on disk.

Finished months become ``era5_surface_box_YYYYMM.csv`` (~10 MB) holding
``valid_time_utc``, ``latitude_deg``, ``longitude_deg`` and the five truth
channels including ``tp_6h_mm`` at the four six-hourly boundary hours. Join
with ``pair_gfs_region.py``. CDS needs the VPN up; the default is a dry run,
add --execute to submit. Re-running skips finished months and valid ZIPs.
"""

from __future__ import annotations

import argparse
import json
import shutil
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
SURFACE_VARIABLES = ["2m_temperature", "mean_sea_level_pressure"]
WIND_VARIABLES = ["10m_u_component_of_wind", "10m_v_component_of_wind"]
PRECIP_VARIABLES = ["total_precipitation"]
INSTANT_TIMES = ["00:00", "06:00", "12:00", "18:00"]
HOURLY_TIMES = [f"{hour:02d}:00" for hour in range(24)]
EVENING_TIMES = [f"{hour:02d}:00" for hour in range(18, 24)]
TRUTH_COLUMNS = (
    "valid_time_utc", "latitude_deg", "longitude_deg",
    "t2m_K", "msl_Pa", "u10_m_s", "v10_m_s", "tp_6h_mm",
)
KIND_VARIABLES = {
    "surface": SURFACE_VARIABLES,
    "wind": WIND_VARIABLES,
    "precip": PRECIP_VARIABLES,
    "evening": PRECIP_VARIABLES,
}


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


def month_days(year: int, month: int) -> list[int]:
    if month == 12:
        following = date(year + 1, 1, 1)
    else:
        following = date(year, month + 1, 1)
    return list(range(1, (following - date(year, month, 1)).days + 1))


def build_payload(kind: str, year: int, month: int) -> dict:
    """CDS request for one month/kind inside the region box."""
    if kind in ("surface", "wind"):
        payload_year, payload_month, days, times = year, month, month_days(year, month), INSTANT_TIMES
    elif kind == "precip":
        payload_year, payload_month, days, times = year, month, month_days(year, month), HOURLY_TIMES
    else:  # evening: last day of the previous month, 18-23 UTC
        if month == 1:
            payload_year, payload_month = year - 1, 12
        else:
            payload_year, payload_month = year, month - 1
        days, times = [month_days(payload_year, payload_month)[-1]], EVENING_TIMES
    return {
        "product_type": ["reanalysis"],
        "variable": KIND_VARIABLES[kind],
        "year": [f"{payload_year:04d}"],
        "month": [f"{payload_month:02d}"],
        "day": [f"{day:02d}" for day in days],
        "time": times,
        "area": AREA,
        "data_format": "netcdf",
        "download_format": "zip",
    }


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

    def submit(self, kind: str, year: int, month: int, target: Path) -> Path:
        payload = build_payload(kind, year, month)
        partial = target.with_suffix(".part")
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
                delay = min(60 * attempt, 300)
                print(f"  {year}-{month:02d}/{kind} attempt {attempt} failed "
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


def validate_finite(nc_path: Path, names: tuple[str, ...]) -> None:
    with nc.Dataset(str(nc_path)) as dataset:
        for name in names:
            grid = np.ma.filled(dataset.variables[name][:], np.nan)
            if not np.isfinite(grid).all():
                raise RuntimeError(f"{nc_path.name}:{name} contains non-finite values")


def truth_frame(surface_nc: Path, wind_nc: Path, precip_ncs: list[Path]) -> pd.DataFrame:
    with nc.Dataset(str(surface_nc)) as surface, nc.Dataset(str(wind_nc)) as wind:
        stamps, latitudes, longitudes, surface_values = read_grid(surface, ("t2m", "msl"))
        wind_stamps, wind_lat, wind_lon, wind_values = read_grid(wind, ("u10", "v10"))
    if not (np.allclose(wind_lat, latitudes) and np.allclose(wind_lon, longitudes)):
        raise ValueError("wind grid does not match the surface grid")
    frames = []
    for precip_nc in precip_ncs:
        with nc.Dataset(str(precip_nc)) as precip:
            precip_stamps, precip_lat, precip_lon, precip_values = read_grid(precip, ("tp",))
        if not (np.allclose(precip_lat, latitudes) and np.allclose(precip_lon, longitudes)):
            raise ValueError("precipitation grid does not match the surface grid")
        frames.append((precip_stamps, precip_values["tp"]))
    hourly = pd.concat(
        [pd.DataFrame({"tp": grid.reshape(len(stamps), -1)}, index=stamps) for stamps, grid in frames]
    )
    hourly = hourly[~hourly.index.duplicated(keep="first")].sort_index()

    instant = pd.DataFrame(
        {name: grid.reshape(len(stamps), -1) for name, grid in
         ({"t2m": surface_values["t2m"], "msl": surface_values["msl"],
           "u10": wind_values["u10"], "v10": wind_values["v10"]}).items()},
        index=stamps,
    )
    combined = instant.join(hourly, how="outer").sort_index()

    consecutive = combined.index.to_series().diff().eq(pd.Timedelta(hours=1))
    six_consecutive = consecutive.rolling(5, min_periods=5).sum().eq(5)
    tp_6h = combined["tp"].rolling(6, min_periods=6).sum().where(six_consecutive) * 1000.0

    boundary = combined.loc[combined.index.hour.isin((0, 6, 12, 18))]
    tp_boundary = tp_6h.reindex(boundary.index)
    result = boundary.rename(columns={"t2m": "t2m_K", "msl": "msl_Pa",
                                      "u10": "u10_m_s", "v10": "v10_m_s"})
    result["tp_6h_mm"] = tp_boundary

    rows = []
    lat_round = np.round(latitudes, 3)
    lon_round = np.round(longitudes, 3)
    arrays = {name: np.asarray(result[name], dtype=np.float64) for name in TRUTH_COLUMNS[3:]}
    for time_index, stamp in enumerate(result.index):
        for lat_index, latitude in enumerate(lat_round):
            for lon_index, longitude in enumerate(lon_round):
                offset = lat_index * len(lon_round) + lon_index
                rows.append((
                    stamp.strftime("%Y-%m-%dT%H:%M:%SZ"), latitude, longitude,
                    *(array[time_index, offset] for array in arrays.values()),
                ))
    return pd.DataFrame(rows, columns=TRUTH_COLUMNS)


def extract_month(year: int, month: int, zips: dict[str, Path], target: Path) -> int:
    with tempfile.TemporaryDirectory(prefix=f"era5_box_{year}{month:02d}_") as work:
        work_dir = Path(work)
        surface_nc = extract_zip(zips["surface"], work_dir)
        wind_nc = extract_zip(zips["wind"], work_dir)
        precip_nc = extract_zip(zips["precip"], work_dir)
        evening_nc = extract_zip(zips["evening"], work_dir)
        validate_finite(surface_nc, ("t2m", "msl"))
        validate_finite(wind_nc, ("u10", "v10"))
        validate_finite(precip_nc, ("tp",))
        validate_finite(evening_nc, ("tp",))
        frame = truth_frame(surface_nc, wind_nc, [evening_nc, precip_nc])
    frame.to_csv(target, index=False, encoding="utf-8-sig", float_format="%.9g")
    return len(frame)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="first month YYYY-MM")
    parser.add_argument("--end", required=True, help="last month YYYY-MM (inclusive)")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--workers", type=int, default=4,
                        help="concurrent CDS requests (default 4; the queue cap "
                             "rejects larger bursts)")
    parser.add_argument("--execute", action="store_true", help="submit CDS requests")
    parser.add_argument("--retry-attempts", type=int, default=4)
    args = parser.parse_args()

    try:
        months = month_list(args.start, args.end)
    except ValueError as error:
        parser.error(str(error))

    zip_dir = args.output_dir / "zips"
    jobs: list[tuple[str, int, int, Path]] = []
    pending_months = []
    for year, month in months:
        target = args.output_dir / f"era5_surface_box_{year}{month:02d}.csv"
        if target.is_file():
            continue
        pending_months.append((year, month))
        for kind in KIND_VARIABLES:
            zip_target = zip_dir / f"{year}{month:02d}_{kind}.zip"
            if not valid_zip(zip_target):
                jobs.append((kind, year, month, zip_target))
    print(f"{len(months)} month(s) in range, {len(pending_months)} pending, "
          f"{len(jobs)} CDS request(s) to submit.", flush=True)
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
    failures: list[tuple[str, int, int]] = []
    if jobs:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(worker.submit, kind, year, month, target): (kind, year, month)
                for kind, year, month, target in jobs
            }
            for future in as_completed(futures):
                kind, year, month = futures[future]
                try:
                    future.result()
                    print(f"downloaded {year}-{month:02d}/{kind}", flush=True)
                except Exception as error:  # noqa: BLE001
                    failures.append((kind, year, month))
                    print(f"FAILED {year}-{month:02d}/{kind}: "
                          f"{type(error).__name__}: {error}", file=sys.stderr, flush=True)

    completed = 0
    for year, month in pending_months:
        target = args.output_dir / f"era5_surface_box_{year}{month:02d}.csv"
        zips = {kind: zip_dir / f"{year}{month:02d}_{kind}.zip" for kind in KIND_VARIABLES}
        if target.is_file():
            continue
        if not all(valid_zip(path) for path in zips.values()):
            continue
        try:
            started = time.time()
            rows = extract_month(year, month, zips, target)
            completed += 1
            print(f"extracted {year}-{month:02d}: {rows} rows ({time.time() - started:.0f}s)",
                  flush=True)
            for path in zips.values():
                path.unlink(missing_ok=True)
        except Exception as error:  # noqa: BLE001
            print(f"EXTRACT FAILED {year}-{month:02d}: "
                  f"{type(error).__name__}: {error}", file=sys.stderr, flush=True)

    print(f"Done: {completed} month(s) extracted this run; "
          f"{len(failures)} request(s) failed.", flush=True)
    if failures:
        print("Failed requests (re-run the same command to retry):", file=sys.stderr)
        for kind, year, month in sorted(failures, key=lambda item: (item[1], item[2], item[0])):
            print(f"  {year}-{month:02d}/{kind}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

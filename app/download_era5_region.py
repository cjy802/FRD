"""Download ERA5 single-level truth for the live service region box.

One calendar month produces three CDS requests (surface instants, wind
instants, hourly precipitation) restricted to ``AREA`` = 116-122.5E /
30-35.5N — the same 23x27 grid the live FuXi service crops, ~621 points —
plus a small preceding-evening precipitation request so the six-hour totals
at month start do not silently bridge the boundary. The extracted monthly
CSV keeps only the four six-hourly boundary hours (00/06/12/18 UTC) that the
training pairs need.

Each monthly CSV is roughly 10 MB and holds ``valid_time_utc``,
``latitude_deg``, ``longitude_deg`` and the five truth channels including
``tp_6h_mm``. Join it with ``pair_gfs_region.py``. CDS needs the VPN up; the
default is a dry run, add --execute to submit requests. Finished months are
skipped on re-run.
"""

from __future__ import annotations

import argparse
import sys
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
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


def request(year: int, month: int, days: list[int], variables: list[str],
            times: list[str], previous_month: tuple[int, int] | None = None) -> dict:
    year, month = previous_month if previous_month else (year, month)
    return {
        "product_type": ["reanalysis"],
        "variable": variables,
        "year": [f"{year:04d}"],
        "month": [f"{month:02d}"],
        "day": [f"{day:02d}" for day in days],
        "time": times,
        "area": AREA,
        "data_format": "netcdf",
        "download_format": "zip",
    }


def month_days(year: int, month: int) -> list[int]:
    if month == 12:
        following = date(year + 1, 1, 1)
    else:
        following = date(year, month + 1, 1)
    return list(range(1, (following - date(year, month, 1)).days + 1))


def download(client, payload: dict, target: Path, attempts: int) -> Path:
    partial = target.with_suffix(".part")
    for attempt in range(1, attempts + 1):
        try:
            if partial.exists():
                partial.unlink()
            client.retrieve(DATASET, payload, str(partial))
            if not partial.is_file() or not zipfile.is_zipfile(partial):
                raise RuntimeError("CDS output is missing or not a ZIP")
            partial.replace(target)
            return target
        except Exception as error:  # noqa: BLE001
            if partial.exists():
                partial.unlink()
            if attempt == attempts:
                raise
            delay = min(60 * attempt, 300)
            print(f"  transient failure ({type(error).__name__}: {error}); "
                  f"retry in {delay}s", file=sys.stderr, flush=True)
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
        flat = time_index
        for lat_index, latitude in enumerate(lat_round):
            for lon_index, longitude in enumerate(lon_round):
                offset = lat_index * len(lon_round) + lon_index
                rows.append((
                    stamp.strftime("%Y-%m-%dT%H:%M:%SZ"), latitude, longitude,
                    *(array[flat, offset] for array in arrays.values()),
                ))
    return pd.DataFrame(rows, columns=TRUTH_COLUMNS)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="first month YYYY-MM")
    parser.add_argument("--end", required=True, help="last month YYYY-MM (inclusive)")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--execute", action="store_true", help="submit CDS requests")
    parser.add_argument("--retry-attempts", type=int, default=4)
    args = parser.parse_args()

    try:
        months = month_list(args.start, args.end)
    except ValueError as error:
        parser.error(str(error))

    pending = []
    for year, month in months:
        target = args.output_dir / f"era5_surface_box_{year}{month:02d}.csv"
        if target.is_file():
            print(f"{year}-{month:02d}: {target.name} exists; skip.")
        else:
            pending.append(((year, month), target))
    print(f"{len(pending)} month(s) pending out of {len(months)}.")
    if not args.execute:
        print("DRY RUN: no requests submitted. Add --execute (VPN required).")
        return 0

    try:
        import cdsapi
    except ImportError:
        print('python -m pip install "cdsapi>=0.7.7"', file=sys.stderr)
        return 2
    client = cdsapi.Client()

    import tempfile
    failures = []
    for (year, month), target in pending:
        started = time.time()
        try:
            with tempfile.TemporaryDirectory(prefix="era5_box_") as work:
                work_dir = Path(work)
                days = month_days(year, month)
                if month == 1:
                    previous = (year - 1, 12)
                else:
                    previous = (year, month - 1)
                last_evening_day = month_days(*previous)[-1]

                surface_zip = download(client, request(year, month, days, SURFACE_VARIABLES, INSTANT_TIMES),
                                       work_dir / "surface.zip", args.retry_attempts)
                wind_zip = download(client, request(year, month, days, WIND_VARIABLES, INSTANT_TIMES),
                                    work_dir / "wind.zip", args.retry_attempts)
                precip_zip = download(client, request(year, month, days, PRECIP_VARIABLES, HOURLY_TIMES),
                                      work_dir / "precip.zip", args.retry_attempts)
                evening_zip = download(
                    client, request(year, month, [last_evening_day], PRECIP_VARIABLES, EVENING_TIMES,
                                    previous_month=previous),
                    work_dir / "evening.zip", args.retry_attempts,
                )
                surface_nc = extract_zip(surface_zip, work_dir)
                wind_nc = extract_zip(wind_zip, work_dir)
                precip_nc = extract_zip(precip_zip, work_dir)
                evening_nc = extract_zip(evening_zip, work_dir)
                validate_finite(surface_nc, ("t2m", "msl"))
                validate_finite(wind_nc, ("u10", "v10"))
                validate_finite(precip_nc, ("tp",))
                validate_finite(evening_nc, ("tp",))
                frame = truth_frame(surface_nc, wind_nc, [evening_nc, precip_nc])
            args.output_dir.mkdir(parents=True, exist_ok=True)
            frame.to_csv(target, index=False, encoding="utf-8-sig", float_format="%.9g")
            print(f"{year}-{month:02d}: {len(frame)} rows -> {target.name} "
                  f"({time.time() - started:.0f}s)", flush=True)
        except Exception as error:  # noqa: BLE001
            failures.append((year, month))
            print(f"FAILED {year}-{month:02d}: {type(error).__name__}: {error}",
                  file=sys.stderr, flush=True)
    if failures:
        print("Failed months (re-run the same command to retry):", file=sys.stderr)
        for year, month in failures:
            print(f"  {year}-{month:02d}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

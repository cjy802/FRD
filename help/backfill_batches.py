"""Backfill a full year of FuXi/ERA5 pairs in monthly batches with bounded disk.

Per calendar month this orchestrator:

1. downloads the daily ERA5 pressure ZIPs (00/06/12/18 UTC by default) into the batch dir;
2. downloads three ERA5 single-level files for the month: t2m/msl and
   u10/v10 at 00/06/12/18 UTC, plus hourly tp. The tp download is split into
   the month itself and the preceding evening (18:00-23:00 of the last day
   of the previous month), because the six-hour accumulations at month
   start need those frames and a single CDS request would fetch whole
   unwanted months; the two pieces are merged into one NetCDF;
3. extracts the hourly point observations and merges them into the year
   observation CSV used for verification;
4. runs build_fuxi_pairs.py once per month (one GPU session), appending
   to the master pairs CSV;
5. deletes the batch directory once every day of the month is paired.

Re-running is safe and resumable: paired days are skipped, finished
downloads are reused, and a month stops at its first failure. The default
is a dry run; add --execute to download and run. Use --download-only with
--execute to retain validated monthly downloads without starting GPU pairing.
Use --daily-cleanup with --execute to pair one day at a time and remove that
day's pressure archive immediately after a successful append.
"""

from __future__ import annotations

import argparse
import calendar
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import download_era5_pressure as pressure_dl
from extract_era5_point import SURFACE_COLUMNS, read_surface


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR
FUXI_PYTHON = Path(sys.executable)
MODEL_DIR = PROJECT_ROOT / "FuXi_EC"
DEFAULT_PAIRS = SCRIPT_DIR / "data" / "fuxi_era5_pairs_6h.csv"
DEFAULT_BATCH_ROOT = SCRIPT_DIR / "backfill"

SINGLE_DATASET = "reanalysis-era5-single-levels"
SURFACE_VARIABLES = ["2m_temperature", "mean_sea_level_pressure"]
WIND_VARIABLES = ["10m_u_component_of_wind", "10m_v_component_of_wind"]
PRECIP_VARIABLES = ["total_precipitation"]
REQUIRED_OBSERVATIONS = ["t2m_K", "u10_m_s", "v10_m_s", "msl_Pa", "tp_6h_mm"]
# CDS request names -> NetCDF variable names inside the delivered files.
NC_VARIABLE_NAMES = {
    "2m_temperature": "t2m",
    "mean_sea_level_pressure": "msl",
    "10m_u_component_of_wind": "u10",
    "10m_v_component_of_wind": "v10",
    "total_precipitation": "tp",
}
PRESSURE_TIMES = list(pressure_dl.FOUR_TIMES)
INSTANT_TIMES = list(PRESSURE_TIMES)
HOURLY_TIMES = [f"{hour:02d}:00" for hour in range(24)]
EVENING_TIMES = [f"{hour:02d}:00" for hour in range(18, 24)]
# Rough per-month download estimate, printed in dry runs only.
PRESSURE_GB_PER_DAY = 0.48
SINGLE_GB_PER_MONTH = 3.0


def month_days(year: int, month: int) -> list[date]:
    return [date(year, month, day) for day in range(1, calendar.monthrange(year, month)[1] + 1)]


def paired_days(pairs_csv: Path, year: int, month: int) -> set[date]:
    """Days of the month already present in the master pairs CSV."""
    if not pairs_csv.is_file():
        return set()
    table = pd.read_csv(pairs_csv, usecols=["init_time_utc", "lead_hours"])
    table = table[table["lead_hours"] == 6]
    stamps = pd.to_datetime(table["init_time_utc"], utc=True, errors="coerce")
    wanted = stamps[
        (stamps.dt.year == year) & (stamps.dt.month == month) & (stamps.dt.hour == 6)
    ]
    return set(stamp.date() for stamp in wanted)


def single_level_request(
    year: int,
    month: int,
    days: list[date],
    variables: list[str],
    times: list[str],
) -> dict[str, list[str] | str]:
    if any(day.year != year or day.month != month for day in days):
        raise ValueError("all days must belong to the requested year/month")
    return {
        "product_type": ["reanalysis"],
        "variable": variables,
        "year": [f"{year:04d}"],
        "month": [f"{month:02d}"],
        "day": sorted({f"{day.day:02d}" for day in days}),
        "time": times,
        "data_format": "netcdf",
        "download_format": "zip",
    }


def download_with_retries(
    dataset: str,
    request: dict[str, list[str] | str],
    target: Path,
    client_factory,
    request_error: type[BaseException],
    attempts: int,
    retry_delay_seconds: float,
) -> None:
    """Download one single-level ZIP atomically, retrying transient failures."""
    partial = target.with_name(f"{target.name}.part")
    transient = (request_error, pressure_dl.InvalidDownloadError)
    for attempt in range(1, attempts + 1):
        if partial.exists():
            partial.unlink()
        try:
            client = client_factory()
            client.retrieve(dataset, request, str(partial))
            if not partial.is_file() or not zipfile.is_zipfile(partial):
                raise pressure_dl.InvalidDownloadError(
                    f"CDS output is missing or not a ZIP: {partial}"
                )
            partial.replace(target)
            return
        except transient as exc:
            if partial.exists():
                partial.unlink()
            if attempt == attempts:
                raise
            delay = min(retry_delay_seconds * (2 ** (attempt - 1)), 300.0)
            print(
                f"Transient failure on attempt {attempt}/{attempts}: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            print(f"Retrying in {delay:g} seconds...", file=sys.stderr)
            time.sleep(delay)


def extract_single_nc(archive_path: Path, target: Path) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        candidates = [
            entry for entry in archive.infolist() if entry.filename.lower().endswith(".nc")
        ]
        if len(candidates) != 1:
            raise ValueError(f"Expected exactly one NetCDF in {archive_path}; found {len(candidates)}")
        with archive.open(candidates[0]) as source, target.open("xb") as destination:
            shutil.copyfileobj(source, destination, length=8 * 1024 * 1024)


def file_is_finite(
    nc_path: Path,
    variables: list[str],
    expected_frames: int | None = None,
) -> bool:
    """True when every frame of every variable is finite and the frame count matches.

    CDS occasionally delivers NetCDF files that are empty, partially filled
    with NaN fields, or truncated, so the time axis length is checked against
    the request as well.
    """
    import netCDF4 as nc

    if not nc_path.is_file():
        return False
    try:
        with nc.Dataset(nc_path) as dataset:
            for name in variables:
                nc_name = NC_VARIABLE_NAMES.get(name, name)
                if nc_name not in dataset.variables:
                    print(
                        f"WARNING: {nc_path.name} lacks variable {nc_name}",
                        file=sys.stderr,
                    )
                    return False
                variable = dataset.variables[nc_name]
                if variable.shape[0] == 0:
                    print(
                        f"WARNING: {nc_path.name} has zero frames for {nc_name}",
                        file=sys.stderr,
                    )
                    return False
                if expected_frames is not None and variable.shape[0] != expected_frames:
                    print(
                        f"WARNING: {nc_path.name} has {variable.shape[0]} {nc_name} frames; "
                        f"expected {expected_frames}",
                        file=sys.stderr,
                    )
                    return False
                for index in range(variable.shape[0]):
                    frame = np.ma.filled(variable[index], np.nan)
                    if not np.isfinite(frame).all():
                        print(
                            f"WARNING: {nc_path.name} has non-finite {nc_name} values "
                            f"at frame {index}",
                            file=sys.stderr,
                        )
                        return False
    except OSError as exc:
        print(f"WARNING: {nc_path.name} cannot be opened: {exc}", file=sys.stderr)
        return False
    return True


def download_and_extract(
    request: dict[str, list[str] | str],
    target_nc: Path,
    variables: list[str],
    args: argparse.Namespace,
    client_factory,
    request_error: type[BaseException],
    expected_frames: int | None = None,
) -> None:
    """Download one single-level ZIP next to the target and extract the NetCDF.

    CDS occasionally delivers NetCDF files whose time axis is complete but
    whose fields are all NaN for part of the period (or empty entirely), so
    the result is validated frame by frame and re-downloaded once if it does
    not match the request.
    """
    for attempt in (1, 2):
        if file_is_finite(target_nc, variables, expected_frames):
            if attempt == 1:
                print("Existing NetCDF; will skip:", target_nc.name)
            return
        if target_nc.exists():
            print(f"Discarding non-finite NetCDF: {target_nc.name}", file=sys.stderr)
            target_nc.unlink()
        target_zip = target_nc.with_suffix(".zip")
        download_with_retries(
            SINGLE_DATASET,
            request,
            target_zip,
            client_factory,
            request_error,
            args.retry_attempts,
            args.retry_delay_seconds,
        )
        print(f"Saved {target_zip} ({target_zip.stat().st_size:,} bytes)")
        extract_single_nc(target_zip, target_nc)
        target_zip.unlink()
    raise RuntimeError(f"NetCDF still non-finite after re-download: {target_nc}")


def merge_precip(previous_nc: Path, month_nc: Path, target: Path) -> None:
    """Concatenate the previous-evening and month tp files along valid_time.

    Only valid_time/latitude/longitude/tp are carried over; ERA5 bookkeeping
    variables such as expver are not needed downstream.
    """
    import netCDF4 as nc

    if target.exists():
        raise FileExistsError(target)
    with nc.Dataset(previous_nc) as head, nc.Dataset(month_nc) as body:
        with nc.Dataset(target, "w", format="NETCDF4") as out:
            latitude = np.asarray(head.variables["latitude"][:])
            longitude = np.asarray(head.variables["longitude"][:])
            out.createDimension("valid_time", None)
            out.createDimension("latitude", len(latitude))
            out.createDimension("longitude", len(longitude))

            time_out = out.createVariable(
                "valid_time", head.variables["valid_time"].dtype, ("valid_time",)
            )
            time_out.setncatts(
                {key: head.variables["valid_time"].getncattr(key)
                 for key in head.variables["valid_time"].ncattrs()}
            )
            for name, values in (("latitude", latitude), ("longitude", longitude)):
                variable = out.createVariable(name, head.variables[name].dtype, (name,))
                variable.setncatts(
                    {key: head.variables[name].getncattr(key)
                     for key in head.variables[name].ncattrs()}
                )
                variable[:] = values

            head_tp = head.variables["tp"]
            body_tp = body.variables["tp"]
            if head_tp.dtype != body_tp.dtype:
                raise ValueError("tp dtypes differ between the two precip files")
            tp = out.createVariable(
                "tp",
                head_tp.dtype,
                ("valid_time", "latitude", "longitude"),
                zlib=True,
                complevel=1,
                chunksizes=(1, len(latitude), len(longitude)),
            )
            tp.setncatts({key: head_tp.getncattr(key) for key in head_tp.ncattrs()})

            stamps = np.concatenate(
                (
                    np.asarray(head.variables["valid_time"][:]).reshape(-1),
                    np.asarray(body.variables["valid_time"][:]).reshape(-1),
                )
            )
            time_out[:] = stamps
            head_count = head.dimensions["valid_time"].size
            body_count = body.dimensions["valid_time"].size
            for index in range(head_count):
                tp[index] = head_tp[index]
            for index in range(body_count):
                tp[head_count + index] = body_tp[index]
    with nc.Dataset(target) as check:
        values = np.asarray(check.variables["valid_time"][:]).reshape(-1)
        if len(values) != head_count + body_count:
            raise ValueError("Merged precip time count is wrong")
        if len(values) != len(np.unique(values)) or np.any(np.diff(values) <= 0):
            raise ValueError("Merged precip times are duplicated or not increasing")
    print(f"Merged {head_count}+{body_count} tp frames -> {target.name}")


def month_observations(
    surface_nc: Path,
    wind_nc: Path,
    precip_nc: Path,
    latitude: float,
    longitude: float,
    year: int,
    month: int,
) -> pd.DataFrame:
    """Hourly point observations for one month, trimmed to the month window."""
    import netCDF4 as nc

    with nc.Dataset(surface_nc) as surface, nc.Dataset(wind_nc) as wind, nc.Dataset(precip_nc) as precip:
        table = read_surface(surface, wind, precip, latitude, longitude)
    index = pd.to_datetime(table["valid_time_utc"], utc=True)
    start = pd.Timestamp(date(year, month, 1), tz="UTC")
    end = pd.Timestamp(
        date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1), tz="UTC"
    )
    return table[(index >= start) & (index < end)].reset_index(drop=True)


def merge_observations(obs_csv: Path, month_table: pd.DataFrame) -> None:
    if list(month_table.columns) != list(SURFACE_COLUMNS):
        raise ValueError("Observation columns do not match the point CSV schema")
    if obs_csv.is_file():
        existing = pd.read_csv(obs_csv)
        if list(existing.columns) != list(SURFACE_COLUMNS):
            raise ValueError(f"Unexpected schema in existing observation CSV: {obs_csv}")
        combined = pd.concat([existing, month_table], ignore_index=True)
    else:
        combined = month_table
    combined = combined.drop_duplicates(subset="valid_time_utc", keep="last")
    combined = combined.sort_values("valid_time_utc")
    obs_csv.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(obs_csv, index=False, encoding="utf-8-sig", float_format="%.9g")
    print(f"Observation CSV now {len(combined)} rows: {obs_csv}")


def run_pairing(
    batch_dir: Path,
    surface_nc: Path,
    wind_nc: Path,
    precip_nc: Path,
    obs_csv: Path,
    args: argparse.Namespace,
    max_runs: int,
) -> None:
    command = [
        str(args.fuxi_python),
        str(SCRIPT_DIR / "build_fuxi_pairs.py"),
        "--pressure-dir", str(batch_dir),
        "--surface", str(surface_nc),
        "--wind", str(wind_nc),
        "--precip", str(precip_nc),
        "--model-dir", str(args.model_dir),
        "--observations", str(obs_csv),
        "--output", str(args.pairs_csv),
        "--latitude", str(args.latitude),
        "--longitude", str(args.longitude),
        "--run",
        "--max-runs", str(max_runs),
    ]
    print("Pairing command:", " ".join(command))
    result = subprocess.run(command)
    if result.returncode != 0:
        raise RuntimeError(f"build_fuxi_pairs.py exited with {result.returncode}")


def pair_one_day_and_cleanup(
    day: date,
    batch_dir: Path,
    surface_nc: Path,
    wind_nc: Path,
    precip_nc: Path,
    args: argparse.Namespace,
    client_factory,
    request_error: type[BaseException],
) -> None:
    """Download, pair, and clean one day's pressure archive."""
    day_dir = batch_dir / f"pressure_{day:%Y%m%d}"
    day_dir.mkdir(parents=True, exist_ok=True)
    target = pressure_dl.target_path(day_dir, day, args.pressure_times)
    if target.exists() and zipfile.is_zipfile(target):
        print(f"Existing pressure ZIP; will skip: {target}")
    else:
        pressure_dl.download_with_retries(
            day=day,
            target=target,
            client_factory=client_factory,
            request_error=request_error,
            attempts=args.retry_attempts,
            retry_delay_seconds=args.retry_delay_seconds,
            times=args.pressure_times,
        )
        print(f"Saved {target} ({target.stat().st_size:,} bytes)")

    run_pairing(day_dir, surface_nc, wind_nc, precip_nc, args.obs_csv, args, 1)
    if day not in paired_days(args.pairs_csv, day.year, day.month):
        raise RuntimeError(
            f"{day} did not appear in the pair CSV after pairing; keeping {day_dir} for retry"
        )
    if not args.keep_downloads:
        shutil.rmtree(day_dir)
        print(f"Day complete; removed {day_dir}")


def process_month(
    year: int,
    month: int,
    args: argparse.Namespace,
    client_factory,
    request_error: type[BaseException],
) -> None:
    days = month_days(year, month)
    batch_dir = args.batch_root / f"{year}-{month:02d}"
    done = paired_days(args.pairs_csv, year, month)
    missing = [day for day in days if day not in done]
    print(f"\n=== {year}-{month:02d}: {len(days)} days, paired {len(done)}/{len(days)} ===")
    if not missing:
        print("Month already complete.")
        if args.execute and not args.keep_downloads and batch_dir.is_dir():
            shutil.rmtree(batch_dir)
            print(f"Removed leftover batch dir {batch_dir}")
        return

    if args.execute:
        args.batch_root.mkdir(parents=True, exist_ok=True)
        free_gb = shutil.disk_usage(args.batch_root).free / 1e9
        if free_gb < args.min_free_gb:
            raise RuntimeError(
                f"Only {free_gb:.1f} GB free on {args.batch_root.anchor}; "
                f"refusing to start the batch (need {args.min_free_gb} GB)"
            )
        batch_dir.mkdir(parents=True, exist_ok=True)

        if not args.daily_cleanup:
            for day in missing:
                target = pressure_dl.target_path(batch_dir, day, args.pressure_times)
                if target.exists() and zipfile.is_zipfile(target):
                    print(f"Existing pressure ZIP; will skip: {target.name}")
                    continue
                pressure_dl.download_with_retries(
                    day=day,
                    target=target,
                    client_factory=client_factory,
                    request_error=request_error,
                    attempts=args.retry_attempts,
                    retry_delay_seconds=args.retry_delay_seconds,
                    times=args.pressure_times,
                )
                print(f"Saved {target} ({target.stat().st_size:,} bytes)")

        surface_nc = batch_dir / f"single_surface_{year}{month:02d}.nc"
        wind_nc = batch_dir / f"single_wind_{year}{month:02d}.nc"
        precip_nc = batch_dir / f"precip_{year}{month:02d}.nc"
        download_and_extract(
            single_level_request(year, month, days, SURFACE_VARIABLES, INSTANT_TIMES),
            surface_nc, SURFACE_VARIABLES, args, client_factory, request_error,
            expected_frames=len(days) * len(INSTANT_TIMES),
        )
        download_and_extract(
            single_level_request(year, month, days, WIND_VARIABLES, INSTANT_TIMES),
            wind_nc, WIND_VARIABLES, args, client_factory, request_error,
            expected_frames=len(days) * len(INSTANT_TIMES),
        )
        previous_day = date(year, month, 1) - timedelta(days=1)
        previous_nc = batch_dir / f"precip_prev_{previous_day:%Y%m%d}.nc"
        month_only_nc = batch_dir / f"precip_month_{year}{month:02d}.nc"
        merged_frames = len(days) * len(HOURLY_TIMES) + len(EVENING_TIMES)
        for attempt in (1, 2):
            if file_is_finite(precip_nc, PRECIP_VARIABLES, merged_frames):
                break
            if precip_nc.exists():
                print(f"Discarding non-finite NetCDF: {precip_nc.name}", file=sys.stderr)
                precip_nc.unlink()
            if attempt == 2:
                raise RuntimeError(f"precip still invalid after re-download: {precip_nc}")
            if not file_is_finite(previous_nc, PRECIP_VARIABLES) and previous_nc.exists():
                previous_nc.unlink()
            download_and_extract(
                single_level_request(
                    previous_day.year,
                    previous_day.month,
                    [previous_day],
                    PRECIP_VARIABLES,
                    EVENING_TIMES,
                ),
                previous_nc, PRECIP_VARIABLES, args, client_factory, request_error,
                expected_frames=len(EVENING_TIMES),
            )
            if not file_is_finite(month_only_nc, PRECIP_VARIABLES) and month_only_nc.exists():
                month_only_nc.unlink()
            download_and_extract(
                single_level_request(year, month, days, PRECIP_VARIABLES, HOURLY_TIMES),
                month_only_nc, PRECIP_VARIABLES, args, client_factory, request_error,
                expected_frames=len(days) * len(HOURLY_TIMES),
            )
            merge_precip(previous_nc, month_only_nc, precip_nc)
        previous_nc.unlink(missing_ok=True)
        month_only_nc.unlink(missing_ok=True)

        month_table = month_observations(
            surface_nc, wind_nc, precip_nc, args.latitude, args.longitude, year, month
        )
        noon = month_table[pd.to_datetime(month_table["valid_time_utc"], utc=True).dt.hour == 12]
        absent = sorted(set(missing) - set(
            pd.to_datetime(noon["valid_time_utc"], utc=True).dt.date
        ))
        if absent:
            raise RuntimeError(f"Observation CSV rows missing for: {absent}")
        incomplete = noon[noon[REQUIRED_OBSERVATIONS].isna().any(axis=1)]
        if len(incomplete):
            raise RuntimeError(
                "Observation rows with missing values at 12:00 UTC: "
                f"{incomplete['valid_time_utc'].tolist()}"
            )
        merge_observations(args.obs_csv, month_table)

        if args.download_only:
            print(
                f"Download-only complete; keeping validated batch dir {batch_dir}. "
                "No GPU pairing was started."
            )
            return

        if args.daily_cleanup:
            for day in missing:
                pair_one_day_and_cleanup(
                    day, batch_dir, surface_nc, wind_nc, precip_nc,
                    args, client_factory, request_error,
                )
        else:
            run_pairing(batch_dir, surface_nc, wind_nc, precip_nc, args.obs_csv, args, len(missing))

        still_missing = [day for day in days if day not in paired_days(args.pairs_csv, year, month)]
        if still_missing:
            raise RuntimeError(
                f"{year}-{month:02d} still missing {len(still_missing)} pair(s): "
                f"{still_missing}; keeping batch dir for retry"
            )
        if not args.keep_downloads:
            shutil.rmtree(batch_dir)
            print(f"Batch complete; removed {batch_dir}")
        else:
            print("Batch complete; downloads kept (--keep-downloads).")
    else:
        estimate = len(missing) * PRESSURE_GB_PER_DAY + SINGLE_GB_PER_MONTH
        print(
            f"DRY RUN: would download {len(missing)} pressure ZIPs + 3 single-level "
            f"requests (~{estimate:.0f} GB) into {batch_dir},"
        )
        print(f"extract observations into {args.obs_csv}, pair {len(missing)} days, then "
              f"{'keep' if args.keep_downloads else 'delete'} the batch dir.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, default=2024)
    parser.add_argument("--months", type=int, nargs="+", default=[],
                        help="calendar months to process (default: all)")
    parser.add_argument("--batch-root", type=Path, default=DEFAULT_BATCH_ROOT)
    parser.add_argument("--pairs-csv", type=Path, default=DEFAULT_PAIRS)
    parser.add_argument("--obs-csv", type=Path,
                        help="year observation CSV (default: point_data/..._<year>.csv)")
    parser.add_argument("--latitude", type=float, default=31.5)
    parser.add_argument("--longitude", type=float, default=119.5)
    parser.add_argument("--model-dir", type=Path, default=MODEL_DIR)
    parser.add_argument("--fuxi-python", type=Path, default=FUXI_PYTHON)
    parser.add_argument(
        "--pressure-times", nargs="+", default=list(PRESSURE_TIMES),
        help="pressure snapshots per day; default: 00:00 06:00 12:00 18:00",
    )
    parser.add_argument("--min-free-gb", type=float, default=30.0)
    parser.add_argument("--retry-attempts", type=int, default=5)
    parser.add_argument("--retry-delay-seconds", type=float, default=30.0)
    parser.add_argument("--keep-downloads", action="store_true",
                        help="keep batch downloads after a successful month")
    parser.add_argument(
        "--download-only", action="store_true",
        help="with --execute, download and validate monthly inputs but skip GPU pairing and cleanup",
    )
    parser.add_argument(
        "--daily-cleanup", action="store_true",
        help="with --execute, pair one day at a time and delete that day's pressure archive after success",
    )
    parser.add_argument("--execute", action="store_true",
                        help="actually download, infer, and clean up (default: dry run)")
    args = parser.parse_args()
    if args.download_only and not args.execute:
        parser.error("--download-only requires --execute")
    if args.download_only and args.daily_cleanup:
        parser.error("--download-only cannot be combined with --daily-cleanup")
    if not args.obs_csv:
        stem = (
            f"era5_surface_point_{args.latitude:g}N_{args.longitude:g}E_{args.year}"
        ).replace(".", "p")
        args.obs_csv = SCRIPT_DIR / "data" / f"{stem}.csv"
    try:
        args.pressure_times = pressure_dl.normalize_times(args.pressure_times)
    except ValueError as exc:
        parser.error(str(exc))
    months = sorted(set(args.months)) if args.months else list(range(1, 13))
    if not (1 <= min(months) and max(months) <= 12):
        parser.error("months must be between 1 and 12")

    print(f"Year {args.year}, months {months}")
    print(f"Pairs CSV: {args.pairs_csv}")
    print(f"Observation CSV: {args.obs_csv}")
    print(f"Batch root: {args.batch_root}")
    print(f"Pressure snapshots: {', '.join(args.pressure_times)}")

    client_factory = None
    request_error = None
    if args.execute:
        try:
            import cdsapi
            import requests
        except ImportError:
            print(
                'Install CDS API in this environment: python -m pip install "cdsapi>=0.7.7"',
                file=sys.stderr,
            )
            return 2
        client_factory = cdsapi.Client
        request_error = requests.exceptions.RequestException
        if not args.download_only:
            build_script = SCRIPT_DIR / "build_fuxi_pairs.py"
            required = (args.fuxi_python, args.model_dir / "short.onnx", args.model_dir / "short", build_script)
            missing = [path for path in required if not path.exists()]
            if missing:
                raise FileNotFoundError(
                    "Full FuXi pairing requires these paths: "
                    + ", ".join(map(str, missing))
                )

    for month in months:
        try:
            process_month(args.year, month, args, client_factory, request_error)
        except Exception as exc:  # noqa: BLE001 - stop the run at the first failure
            import traceback
            traceback.print_exc()
            print(f"FAILED {args.year}-{month:02d}: {type(exc).__name__}: {exc}", file=sys.stderr)
            print("Fix the cause and re-run; completed work is kept.", file=sys.stderr)
            return 1
    print("\nAll requested months processed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

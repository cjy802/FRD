"""Backfill production-style regional FuXi-2.1 forecasts from NOAA GFS history.

For every requested initialization (06 UTC by default) this downloads the two
GFS analysis frames from the public AWS bucket (no VPN needed), builds the
70-channel input, runs the production worker ``predict_fuxi_point.py`` with
the FuXi-2.1 ONNX model, and appends every regional grid point of the live
service box (116-122.5E, 30-35.5N, 0.25 deg, 23x27 = 621 points) for leads
6/12/18/24 to one CSV. ERA5 truth is NOT fetched here; run
``download_era5_region.py`` separately and join with ``pair_gfs_region.py``.

Transpacific single connections to S3 stall below 100 KB/s, so byte-range
groups inside one file are fetched with a small thread pool, and upcoming
dates are prefetched while the GPU infers the current one. Peak disk stays
around 1-2 GB: prefetch depth inputs plus their GRIBs, deleted after use.
The default is a dry run; add --execute to run. Re-running is safe: dates
already present in the output CSV are skipped.
"""

from __future__ import annotations

import argparse
import csv
import json
import queue
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import netCDF4 as nc
import numpy as np
import requests

from build_fuxi_pairs import FIELD_MAP
from prepare_fuxi1_input import CHANNELS
from prepare_gfs_live import GFSDownloader, grib_arrays, native_nc_path, select_records, source_url


APP_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = APP_ROOT.parent
DEFAULT_OUTPUT = PROJECT_ROOT / "FuXi/DATA/fuxi2_gfs_region_forecast.csv"
DEFAULT_CACHE = PROJECT_ROOT / "FuXi/DATA/gfs_cache"
DEFAULT_INPUTS = APP_ROOT / "prepared/backfill_inputs"
DEFAULT_MODEL_DIR = PROJECT_ROOT / "FuXi_EC"
DEFAULT_PYTHON = Path("D:/anaconda/envs/fuxi_onnx/python.exe")
if not DEFAULT_PYTHON.is_file():
    # Non-Windows hosts (cloud GPU boxes) run the worker with this interpreter.
    import sys as _sys

    DEFAULT_PYTHON = Path(_sys.executable)
MODEL_NAME = "FuXi-2.1-short"
RECORDED_LEADS = (6, 12, 18, 24)
FORECAST_COLUMNS = (
    "init_time_utc", "valid_time_utc", "lead_hours",
    "latitude_deg", "longitude_deg", "model",
) + tuple(f"fuxi_{field}" for field, _, _ in FIELD_MAP)
CHANNEL_INDEX = {field: CHANNELS.index(channel) for field, channel, _ in FIELD_MAP}


def parse_dates(args: argparse.Namespace) -> list[date]:
    first = date.fromisoformat(args.start)
    last = date.fromisoformat(args.end)
    if last < first:
        raise ValueError("--end must not precede --start")
    dates = []
    current = first
    while current <= last:
        dates.append(current)
        current += timedelta(days=args.every_days)
    return dates


def stamp(moment: datetime) -> str:
    """Normalize worker ISO stamps ("+00:00") to the table convention ("Z")."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def done_dates(output: Path) -> set[date]:
    if not output.is_file():
        return set()
    import pandas as pd

    table = pd.read_csv(output, usecols=["init_time_utc"])
    stamps = pd.to_datetime(table["init_time_utc"], utc=True, errors="coerce")
    return set(stamp.date() for stamp in stamps.dropna())


class ParallelFetcher:
    """Fetch one GFS GRIB subset with concurrent byte-range requests.

    Produces exactly the cache files and manifests that
    ``GFSDownloader.fetch_records`` writes, so partially downloaded dates
    resume transparently and the live system could reuse them if pointed at
    this cache.
    """

    def __init__(self, downloader: GFSDownloader, workers: int):
        self.downloader = downloader
        self.workers = workers
        self.local = threading.local()

    def session(self) -> requests.Session:
        if not hasattr(self.local, "session"):
            session = requests.Session()
            session.headers["User-Agent"] = "LowAltitude-FuXi/1.0 (NOAA public initial fields)"
            self.local.session = session
        return self.local.session

    def fetch_group(self, url: str, group: dict) -> bytearray:
        start, end = group["start"], group["end"]
        interval = f"bytes={start}-{end if end is not None else ''}"
        delays = (3, 9, 27, 60)
        for attempt in range(5):
            try:
                with self.session().get(url, headers={"Range": interval}, stream=True,
                                        timeout=(15, 120)) as response:
                    response.raise_for_status()
                    if response.status_code != 206:
                        raise ValueError("GFS server ignored the byte range")
                    content_range = response.headers.get("Content-Range", "")
                    if not content_range.startswith(f"bytes {start}-"):
                        raise ValueError("GFS server returned the wrong byte range")
                    payload = bytearray()
                    for chunk in response.iter_content(1024 * 1024):
                        payload.extend(chunk)
                        if len(payload) > 40 * 1024 * 1024:
                            raise ValueError("GFS message exceeds the expected bound")
                if end is not None and len(payload) != end - start + 1:
                    raise ValueError("Incomplete GFS byte range")
                if payload[:4] != b"GRIB" or payload[-4:] != b"7777":
                    raise ValueError("GFS response is not a complete GRIB message")
                return payload
            except (requests.RequestException, ValueError):
                if attempt == 4:
                    raise
                time.sleep(delays[min(attempt, len(delays) - 1)])

    def fetch(self, cycle: datetime, hour: int, kind: str, cache: Path) -> Path:
        records = select_records(self.downloader.inventory(cycle, hour), kind)
        url = source_url(cycle, hour)
        target = cache / f"gfs_{cycle:%Y%m%d%H}_f{hour:03d}_{kind}.grib2"
        manifest = target.with_suffix(".json")
        expected = [{"channel": record["channel"], "start": record["start"], "end": record["end"]}
                    for record in records]
        if target.is_file() and manifest.is_file():
            try:
                info = json.loads(manifest.read_text(encoding="utf-8"))
            except ValueError:
                info = {}
            if info.get("fields") == expected and info.get("bytes") == target.stat().st_size:
                return target
        groups = []
        for record in records:
            if groups and groups[-1]["end"] is not None and groups[-1]["end"] + 1 == record["start"]:
                groups[-1]["end"] = record["end"]
            else:
                groups.append({"start": record["start"], "end": record["end"]})
        cache.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(".part")
        started = time.time()
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            payloads = list(pool.map(lambda group: self.fetch_group(url, group), groups))
        count = sum(len(payload) for payload in payloads)
        print(f"fetched {target.name}: {count / 1e6:.1f} MB in {time.time() - started:.0f}s", flush=True)
        with partial.open("wb") as handle:
            for payload in payloads:
                handle.write(payload)
        partial.replace(target)
        manifest.write_text(json.dumps({"generator": "fuxi-gfs-live", "fields": expected, "bytes": count}),
                            encoding="utf-8")
        return target

    def fetch_fields(self, cycle: datetime, hour: int, kind: str, cache: Path) -> Path:
        return self.fetch(cycle, hour, kind, cache)


def build_input_for_init(fetcher: ParallelFetcher, cache: Path, init: datetime, output: Path) -> list[Path]:
    """Historical-analogue of prepare_gfs_live.prepare_input for one fixed init.

    Historical cycles always have their f000 analysis published on AWS, so no
    short-range background fallback is needed: every frame uses cycle f000 and
    the six-hour rain of the previous cycle.
    """
    previous = init - timedelta(hours=6)
    output.parent.mkdir(parents=True, exist_ok=True)
    sources = []
    paths = []
    used = []
    for valid in (previous, init):
        state = fetcher.fetch_fields(valid, 0, "state", cache)
        rain = fetcher.fetch_fields(valid - timedelta(hours=6), 6, "rain", cache)
        paths.append((valid, state, rain))
        used.extend((state, rain))
        sources.append({
            "valid_time_utc": valid.isoformat(),
            "state_cycle_utc": valid.isoformat(),
            "state_forecast_hours": 0,
            "precipitation_cycle_utc": (valid - timedelta(hours=6)).isoformat(),
            "precipitation_forecast_hours": 6,
        })
    pending = output.with_name(f"{output.name}.pending.nc")
    pending_nc = Path(native_nc_path(pending))
    output_nc = Path(native_nc_path(output))
    try:
        with nc.Dataset(str(pending_nc), "w", format="NETCDF4") as data:
            for name, size in (("time", 2), ("level", 70), ("lat", 721), ("lon", 1440)):
                data.createDimension(name, size)
            time_var = data.createVariable("time", "i8", ("time",))
            time_var.units = "seconds since 1970-01-01 00:00:00"
            time_var.calendar = "proleptic_gregorian"
            time_var[:] = [int(previous.timestamp()), int(init.timestamp())]
            data.createVariable("level", str, ("level",))[:] = np.asarray(CHANNELS, dtype=object)
            data.createVariable("lat", "f8", ("lat",))[:] = np.linspace(90, -90, 721)
            data.createVariable("lon", "f8", ("lon",))[:] = np.arange(1440) * .25
            values = data.createVariable(
                "data", "f4", ("time", "level", "lat", "lon"),
                zlib=True, complevel=1, chunksizes=(1, 1, 721, 1440),
            )
            data.setncattr("init_time_utc", init.isoformat())
            data.setncattr("initialization_source", "NOAA GFS analysis (historical backfill)")
            data.setncattr("source_frames", json.dumps(sources))
            for index, (valid, state, rain) in enumerate(paths):
                written = set()
                for file, kind in ((state, "state"), (rain, "rain")):
                    for channel, grid in grib_arrays(file, valid, kind):
                        if channel in written:
                            raise ValueError(f"Duplicate channel {channel}")
                        values[index, CHANNELS.index(channel)] = grid
                        written.add(channel)
                if written != set(CHANNELS):
                    raise ValueError("Incomplete 70-channel GFS input")
        pending_nc.replace(output_nc)
    finally:
        try:
            pending_nc.unlink(missing_ok=True)
        except OSError:
            pass
    return used


def run_worker(python: Path, input_nc: Path, model_dir: Path, max_lead: int) -> dict:
    command = [
        str(python), "-B", str(APP_ROOT / "predict_fuxi_point.py"),
        "--input", str(input_nc),
        "--model-dir", str(model_dir),
        "--latitude", "31.5", "--longitude", "119.5",
        "--max-lead-hours", str(max_lead),
    ]
    completed = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=1800, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        payload = json.loads(completed.stdout)
    except ValueError as error:
        tail = (completed.stderr or "")[-800:]
        raise RuntimeError(f"worker returned no JSON (exit {completed.returncode}): {tail}") from error
    if completed.returncode or "error" in payload:
        raise RuntimeError(payload.get("error", f"worker exit {completed.returncode}"))
    return payload


def extract_rows(payload: dict, leads: set[int]) -> list[dict]:
    region = payload["region"]
    latitudes = [round(float(value), 3) for value in region["latitudes"]]
    longitudes = [round(float(value), 3) for value in region["longitudes"]]
    init = datetime.fromisoformat(payload["init_time_utc"])
    rows = []
    for snapshot in region["snapshots"]:
        lead = int(snapshot["lead_hours"])
        if lead == 0 or lead not in leads:
            continue
        values = snapshot["values"]
        valid = stamp(datetime.fromisoformat(snapshot["valid_time_utc"]))
        for i, latitude in enumerate(latitudes):
            for j, longitude in enumerate(longitudes):
                row = {
                    "init_time_utc": stamp(init),
                    "valid_time_utc": valid,
                    "lead_hours": lead,
                    "latitude_deg": latitude,
                    "longitude_deg": longitude,
                    "model": MODEL_NAME,
                }
                for field, index in CHANNEL_INDEX.items():
                    row[f"fuxi_{field}"] = round(float(values[index][i][j]), 6)
                rows.append(row)
    if not rows:
        raise ValueError("worker payload contains none of the requested leads")
    return rows


def append_rows(output: Path, rows: list[dict]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    create_header = not output.is_file()
    with output.open("a", encoding="utf-8-sig" if create_header else "utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FORECAST_COLUMNS)
        if create_header:
            writer.writeheader()
        writer.writerows(rows)


def remove_files(paths: list[Path]) -> None:
    for path in paths:
        Path(path).with_suffix(".json").unlink(missing_ok=True)
        Path(path).unlink(missing_ok=True)


def date_cache_files(cache: Path, init: datetime) -> list[Path]:
    files = []
    for valid in (init - timedelta(hours=6), init):
        files.append(cache / f"gfs_{valid:%Y%m%d%H}_f000_state.grib2")
        files.append(cache / f"gfs_{valid - timedelta(hours=6):%Y%m%d%H}_f006_rain.grib2")
    return files


def purge_date_cache(cache: Path, init: datetime) -> None:
    """Drop a date's cached GRIBs so a corrupt download cannot poison retries."""
    remove_files(date_cache_files(cache, init))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="first UTC init date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="last UTC init date YYYY-MM-DD (inclusive)")
    parser.add_argument("--every-days", type=int, default=1)
    parser.add_argument("--init-hour", type=int, default=6, choices=(0, 6, 12, 18))
    parser.add_argument("--leads", nargs="+", type=int, default=list(RECORDED_LEADS),
                        choices=RECORDED_LEADS, help="leads to record (default: 6 12 18 24)")
    parser.add_argument("--max-lead-hours", type=int, default=24, choices=(6, 12, 18, 24),
                        help="worker rollout horizon; must cover --leads")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--inputs-dir", type=Path, default=DEFAULT_INPUTS)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--python", type=Path, default=DEFAULT_PYTHON)
    parser.add_argument("--fetch-workers", type=int, default=6,
                        help="concurrent byte-range requests inside one GRIB file")
    parser.add_argument("--prefetch-depth", type=int, default=2,
                        help="dates prepared ahead of the GPU inference loop")
    parser.add_argument("--execute", action="store_true", help="actually download and run")
    parser.add_argument("--stop-after", type=int, default=0,
                        help="process at most N pending dates then stop (0 = no cap)")
    parser.add_argument("--retry-attempts", type=int, default=3)
    args = parser.parse_args()

    if max(args.leads) > args.max_lead_hours:
        parser.error("--max-lead-hours must cover every --leads value")
    if not args.python.is_file():
        parser.error(f"python not found: {args.python}")
    if not (args.model_dir / "short.onnx").is_file():
        parser.error(f"FuXi-2.1 model not found: {args.model_dir}")

    try:
        dates = parse_dates(args)
    except ValueError as error:
        parser.error(str(error))

    finished = done_dates(args.output)
    pending = [day for day in dates if day not in finished]
    print(f"Selected {len(dates)} date(s); {len(finished)} already in {args.output.name}; "
          f"{len(pending)} pending.", flush=True)
    print(f"Init hour {args.init_hour:02d} UTC, recorded leads {sorted(args.leads)}, "
          f"~621 points/date, transient peak ~1-2 GB.", flush=True)
    if args.stop_after:
        pending = pending[:args.stop_after]
        print(f"--stop-after: capped at {len(pending)} date(s).", flush=True)
    if not pending:
        return 0
    if not args.execute:
        print("DRY RUN: nothing was downloaded. Add --execute to run.")
        return 0

    import shutil
    if shutil.disk_usage(args.output.parent).free < 2 * 1024**3:
        print("Less than 2 GB free on the output drive; refusing to start.", file=sys.stderr)
        return 2

    ready: "queue.Queue[tuple[date, Path | None, list[Path] | None, BaseException | None]]" = queue.Queue()
    slots = threading.BoundedSemaphore(max(1, args.prefetch_depth))

    def produce() -> None:
        downloader = GFSDownloader(args.cache_dir)
        fetcher = ParallelFetcher(downloader, args.fetch_workers)
        for day in pending:
            slots.acquire()
            init = datetime(day.year, day.month, day.day, args.init_hour, tzinfo=timezone.utc)
            input_path = args.inputs_dir / f"input_{day:%Y%m%d}{args.init_hour:02d}.nc"
            error: BaseException | None = None
            for attempt in range(2):
                try:
                    used = build_input_for_init(fetcher, args.cache_dir, init, input_path)
                    ready.put((day, input_path, used, None))
                    error = None
                    break
                except Exception as failure:  # noqa: BLE001 - retried once, then reported
                    error = failure
                    if attempt == 0:
                        print(f"prefetch retry for {day.isoformat()}: "
                              f"{type(failure).__name__}: {failure}", flush=True)
                        purge_date_cache(args.cache_dir, init)
                        time.sleep(30)
            if error is not None:
                ready.put((day, None, None, error))
        ready.put((date(1970, 1, 1), None, None, StopIteration()))

    producer = threading.Thread(target=produce, daemon=True, name="gfs-prefetch")
    producer.start()

    failures: list[date] = []
    processed = 0
    while True:
        day, input_path, used, produce_error = ready.get()
        try:
            if isinstance(produce_error, StopIteration):
                break
            if produce_error is not None:
                raise produce_error
            started = time.time()
            for attempt in range(1, args.retry_attempts + 1):
                try:
                    input_nc = Path(native_nc_path(input_path))
                    payload = run_worker(args.python, input_nc, args.model_dir, args.max_lead_hours)
                    rows = extract_rows(payload, set(args.leads))
                    append_rows(args.output, rows)
                    input_nc.unlink(missing_ok=True)
                    remove_files(used)
                    processed += 1
                    print(f"{day.isoformat()} +{args.init_hour:02d}: {len(rows)} rows appended, "
                          f"{time.time() - started:.0f}s", flush=True)
                    break
                except Exception as error:  # noqa: BLE001 - keep the batch alive per date
                    input_nc_missing = not Path(native_nc_path(input_path)).is_file()
                    if input_nc_missing:
                        # The prefetch product is gone; nothing a retry inside this
                        # process can fix. Rebuild happens on the next full run.
                        raise
                    if attempt == args.retry_attempts:
                        raise
                    print(f"retry {attempt}/{args.retry_attempts - 1} for {day.isoformat()}: "
                          f"{type(error).__name__}: {error}", flush=True)
                    time.sleep(10 * attempt)
        except Exception as error:  # noqa: BLE001
            failures.append(day)
            print(f"FAILED {day.isoformat()}: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
            if input_path is not None:
                Path(native_nc_path(input_path)).unlink(missing_ok=True)
            purge_date_cache(
                args.cache_dir,
                datetime(day.year, day.month, day.day, args.init_hour, tzinfo=timezone.utc),
            )
        finally:
            if not isinstance(produce_error, StopIteration):
                slots.release()
    producer.join(timeout=30)
    print(f"Done: {processed} date(s) processed, {len(failures)} failed.", flush=True)
    if failures:
        print("Failed dates (re-run the same command to retry):", file=sys.stderr)
        for day in failures:
            print(f"  {day.isoformat()}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

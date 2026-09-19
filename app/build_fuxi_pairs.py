"""Build +6h FuXi-1.0 / ERA5 point pairs without saving global forecasts.

One ERA5 pressure ZIP (00/06 UTC) produces one forecast initialized at 06 UTC.
The default is a read-only preview. Use --run to infer, with --max-runs as a
safety limit. Model weights and global source files are never modified.
"""

from __future__ import annotations

import argparse
import csv
import gc
import re
import shutil
import sys
import tempfile
import zipfile
from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import netCDF4 as nc
import onnxruntime as ort
import pandas as pd
import xarray as xr

from prepare_fuxi1_input import CHANNELS, build_input
from run_fuxi1_short import prepare_cuda_libraries


PRESSURE_ZIP_NAME = re.compile(r"era5_pressure_(\d{8})_00_06\.zip$")
FIELD_MAP = (
    ("t2m_K", "T2M", "t2m_K"),
    ("u10_m_s", "U10", "u10_m_s"),
    ("v10_m_s", "V10", "v10_m_s"),
    ("msl_Pa", "MSL", "msl_Pa"),
    ("tp_6h_mm", "TP", "tp_6h_mm"),
)
META_COLUMNS = (
    "init_time_utc", "valid_time_utc", "lead_hours",
    "latitude_deg", "longitude_deg", "model", "pressure_zip",
)
PAIR_COLUMNS = META_COLUMNS + tuple(
    f"{prefix}_{field}"
    for field, _, _ in FIELD_MAP
    for prefix in ("fuxi", "era5", "residual")
)


def discover_pressure_zips(folder: Path, requested_dates: set[date]) -> list[tuple[date, Path]]:
    if not folder.is_dir():
        raise NotADirectoryError(folder)
    found: list[tuple[date, Path]] = []
    for path in sorted(folder.iterdir()):
        match = PRESSURE_ZIP_NAME.fullmatch(path.name)
        if not path.is_file() or match is None:
            continue
        day = datetime.strptime(match.group(1), "%Y%m%d").date()
        if not requested_dates or day in requested_dates:
            found.append((day, path))
    missing = requested_dates - {day for day, _ in found}
    if missing:
        raise FileNotFoundError(f"No pressure ZIP for date(s): {sorted(missing)}")
    if not found:
        raise FileNotFoundError(f"No era5_pressure_YYYYMMDD_00_06.zip files in {folder}")
    return found


def existing_keys(path: Path) -> set[tuple[str, int]]:
    if not path.exists():
        return set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != PAIR_COLUMNS:
            raise ValueError(f"Existing pair CSV has an unexpected schema: {path}")
        keys = [(row["init_time_utc"], int(row["lead_hours"])) for row in reader]
    if len(keys) != len(set(keys)):
        raise ValueError(f"Existing pair CSV contains duplicate (init_time, lead_hours): {path}")
    return set(keys)


def load_observations(path: Path, latitude: float, longitude: float) -> pd.DataFrame:
    required = {"valid_time_utc", "latitude_deg", "longitude_deg"}
    required.update(source for _, _, source in FIELD_MAP)
    observations = pd.read_csv(path)
    missing = required - set(observations.columns)
    if missing:
        raise ValueError(f"Observation table is missing columns: {sorted(missing)}")
    if not (
        np.allclose(observations.latitude_deg, latitude)
        and np.allclose(observations.longitude_deg, longitude)
    ):
        raise ValueError("Observation table does not match the requested grid point")
    observations.index = pd.to_datetime(observations.valid_time_utc, utc=True)
    if observations.index.has_duplicates:
        raise ValueError("Observation table contains duplicate valid times")
    return observations


def source_time_index(path: Path) -> tuple[set[int], str, str]:
    if not path.is_file():
        raise FileNotFoundError(path)
    with nc.Dataset(path) as dataset:
        coordinate = dataset.variables["valid_time"]
        values = {int(value) for value in np.asarray(coordinate[:]).reshape(-1)}
        units = str(coordinate.units)
        calendar = str(getattr(coordinate, "calendar", "standard"))
    return values, units, calendar


def numeric_time(when: datetime, info: tuple[set[int], str, str]) -> int:
    _, units, calendar = info
    return int(nc.date2num(when, units=units, calendar=calendar))


def classify_pending(
    pending: list[tuple[date, Path]],
    args: argparse.Namespace,
    observations: pd.DataFrame,
) -> tuple[list[tuple[date, Path]], list[tuple[date, str]]]:
    source_times = {
        "surface": source_time_index(args.surface),
        "wind": source_time_index(args.wind),
        "precip": source_time_index(args.precip),
    }
    runnable: list[tuple[date, Path]] = []
    blocked: list[tuple[date, str]] = []
    for day, pressure_zip in pending:
        valid_time = datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc)
        reasons: list[str] = []
        if valid_time not in observations.index:
            reasons.append(f"missing point ERA5 at {valid_time.isoformat()}")

        prepared = args.prepared_dir / f"input_{day:%Y%m%d}_06.nc" if args.prepared_dir else None
        if prepared is None or not prepared.is_file():
            frames = (
                datetime(day.year, day.month, day.day, 0),
                datetime(day.year, day.month, day.day, 6),
            )
            for kind in ("surface", "wind"):
                available = source_times[kind][0]
                missing = [when for when in frames if numeric_time(when, source_times[kind]) not in available]
                if missing:
                    reasons.append(f"missing {kind}: {', '.join(when.isoformat() for when in missing)}")
            required_precip = sorted(
                {frame - timedelta(hours=hours_back) for frame in frames for hours_back in range(5, -1, -1)}
            )
            precip_available = source_times["precip"][0]
            missing_precip = [
                when for when in required_precip
                if numeric_time(when, source_times["precip"]) not in precip_available
            ]
            if missing_precip:
                preview = ", ".join(when.isoformat() for when in missing_precip[:3])
                suffix = "..." if len(missing_precip) > 3 else ""
                reasons.append(f"missing precip: {preview}{suffix}")

        if reasons:
            blocked.append((day, "; ".join(reasons)))
        else:
            runnable.append((day, pressure_zip))
    return runnable, blocked


def time_embedding(init_time: datetime) -> np.ndarray:
    """Match original FuXi time_encoding() for a single +6h step."""
    periods = [pd.Period(init_time + timedelta(hours=offset), "h") for offset in (-6, 0, 6)]
    values = np.asarray([(p.day_of_year / 366, p.hour / 24) for p in periods], dtype=np.float32)
    return np.concatenate((np.sin(values), np.cos(values)), axis=-1).reshape(1, 12)


def make_session(model_dir: Path) -> ort.InferenceSession:
    model_path = model_dir / "short.onnx"
    if not model_path.is_file() or not (model_dir / "short").is_file():
        raise FileNotFoundError("short.onnx and its external weight file must be together")
    options = ort.SessionOptions()
    options.enable_cpu_mem_arena = False
    options.enable_mem_pattern = False
    options.enable_mem_reuse = False
    options.intra_op_num_threads = 1
    ort.set_default_logger_severity(3)
    session = ort.InferenceSession(
        str(model_path),
        sess_options=options,
        providers=[("CUDAExecutionProvider", {"arena_extend_strategy": "kSameAsRequested"})],
    )
    if "CUDAExecutionProvider" not in session.get_providers():
        raise RuntimeError("FuXi session did not activate CUDAExecutionProvider")
    session.disable_fallback()
    print("ONNX Runtime providers:", session.get_providers())
    return session


def extract_zip_nc(archive_path: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        candidates = [entry for entry in archive.infolist() if entry.filename.lower().endswith(".nc")]
        if len(candidates) != 1:
            raise ValueError(f"Expected exactly one NetCDF in {archive_path}; found {len(candidates)}")
        with archive.open(candidates[0]) as source, destination.open("xb") as target:
            shutil.copyfileobj(source, target, length=8 * 1024 * 1024)


def input_for_date(day: date, pressure_zip: Path, args: argparse.Namespace, work_dir: Path) -> Path:
    init_time = datetime(day.year, day.month, day.day, 6)
    if args.prepared_dir:
        candidate = args.prepared_dir / f"input_{day:%Y%m%d}_06.nc"
        if candidate.is_file():
            print("Reusing prepared input:", candidate)
            return candidate

    pressure_nc = work_dir / "pressure.nc"
    prepared_nc = work_dir / "fuxi_input.nc"
    extract_zip_nc(pressure_zip, pressure_nc)
    prep_args = argparse.Namespace(
        pressure=pressure_nc,
        surface=args.surface,
        wind=args.wind,
        precip=args.precip,
        init_time=init_time.strftime("%Y-%m-%dT%H:%M"),
        output=prepared_nc,
    )
    build_input(prep_args)
    return prepared_nc


def forecast_pair(
    day: date,
    pressure_zip: Path,
    args: argparse.Namespace,
    session: ort.InferenceSession,
    observations: pd.DataFrame,
) -> dict[str, object]:
    init_time = datetime(day.year, day.month, day.day, 6, tzinfo=timezone.utc)
    valid_time = init_time + timedelta(hours=6)
    if valid_time not in observations.index:
        raise ValueError(f"No ERA5 point reference for {valid_time.isoformat()}")
    obs_row = observations.loc[valid_time]

    existing_input = (
        args.prepared_dir / f"input_{day:%Y%m%d}_06.nc"
        if args.prepared_dir else None
    )
    # A validated prepared input needs no temporary directory. Otherwise the
    # TemporaryDirectory contains only generated copies/input and is cleaned
    # after this date. Original ERA5 files and model weights remain.
    scratch = (
        nullcontext(existing_input)
        if existing_input is not None and existing_input.is_file()
        else tempfile.TemporaryDirectory(prefix="fuxi_pair_")
    )
    with scratch as location:
        if isinstance(location, Path):
            input_path = location
            print("Reusing prepared input:", input_path)
        else:
            input_path = input_for_date(day, pressure_zip, args, Path(location))
        with xr.open_dataarray(input_path) as prepared:
            if prepared.shape != (2, 70, 721, 1440):
                raise ValueError(f"Invalid FuXi input shape: {prepared.shape}")
            if tuple(str(value) for value in prepared.level.values) != CHANNELS:
                raise ValueError("FuXi channel order is incorrect")
            actual_init = pd.Timestamp(prepared.time.values[-1]).to_pydatetime().replace(tzinfo=timezone.utc)
            if actual_init != init_time:
                raise ValueError(f"Input initialization {actual_init} != requested {init_time}")
            latitudes = np.asarray(prepared.lat.values)
            longitudes = np.asarray(prepared.lon.values)
            lat_index = int(np.argmin(np.abs(latitudes - args.latitude)))
            lon_index = int(np.argmin(np.abs(longitudes - args.longitude)))
            if not (
                np.isclose(latitudes[lat_index], args.latitude)
                and np.isclose(longitudes[lon_index], args.longitude)
            ):
                raise ValueError("Requested point is not on the FuXi input grid")
            input_values = np.asarray(prepared.values[None], dtype=np.float32)

        output_values = session.run(None, {"input": input_values, "temb": time_embedding(init_time)})[0]
        if output_values.shape != (1, 2, 70, 721, 1440):
            raise ValueError(f"Unexpected FuXi output shape: {output_values.shape}")
        point_values = output_values[0, -1, :, lat_index, lon_index]
        if not np.isfinite(point_values).all():
            raise ValueError(f"FuXi forecast has non-finite values at {valid_time}")
        del input_values, output_values
        gc.collect()

    row: dict[str, object] = {
        "init_time_utc": init_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "valid_time_utc": valid_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lead_hours": 6,
        "latitude_deg": args.latitude,
        "longitude_deg": args.longitude,
        "model": "FuXi-1.0-short",
        "pressure_zip": pressure_zip.name,
    }
    for field, channel, observation_column in FIELD_MAP:
        forecast = float(point_values[CHANNELS.index(channel)])
        observed = float(obs_row[observation_column])
        if not np.isfinite(observed):
            raise ValueError(f"Missing ERA5 {observation_column} at {valid_time}")
        row[f"fuxi_{field}"] = forecast
        row[f"era5_{field}"] = observed
        row[f"residual_{field}"] = observed - forecast
    return row


def append_pair(path: Path, row: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    create_header = not path.exists()
    with path.open("a", encoding="utf-8-sig" if create_header else "utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PAIR_COLUMNS)
        if create_header:
            writer.writeheader()
        writer.writerow(row)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pressure-dir", type=Path, required=True)
    parser.add_argument("--surface", type=Path, required=True)
    parser.add_argument("--wind", type=Path, required=True)
    parser.add_argument("--precip", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--observations", type=Path, required=True, help="point ERA5 surface CSV")
    parser.add_argument("--output", type=Path, required=True, help="pair CSV; new rows append safely")
    parser.add_argument("--prepared-dir", type=Path, help="reuse validated input_YYYYMMDD_06.nc when present")
    parser.add_argument("--date", action="append", help="YYYY-MM-DD; repeatable, default: all local ZIPs")
    parser.add_argument("--latitude", type=float, default=31.5)
    parser.add_argument("--longitude", type=float, default=119.5)
    parser.add_argument("--run", action="store_true", help="perform GPU inference; default is preview")
    parser.add_argument("--max-runs", type=int, default=1, help="safety cap; default 1")
    args = parser.parse_args()

    selected_dates = {date.fromisoformat(text) for text in (args.date or [])}
    candidates = discover_pressure_zips(args.pressure_dir, selected_dates)
    completed = existing_keys(args.output)
    unpaired = [
        (day, path) for day, path in candidates
        if (f"{day:%Y-%m-%d}T06:00:00Z", 6) not in completed
    ]
    observations = load_observations(args.observations, args.latitude, args.longitude)
    pending, blocked = classify_pending(unpaired, args, observations)
    print(
        f"Pressure ZIPs: {len(candidates)}; already paired: {len(candidates) - len(unpaired)}; "
        f"runnable: {len(pending)}; blocked: {len(blocked)}"
    )
    for day, reason in blocked:
        print(f"  BLOCKED {day.isoformat()}: {reason}")
    for day, path in pending:
        print(f"  {day.isoformat()} 06:00 UTC -> {path.name}")
    if not args.run:
        print("PREVIEW ONLY. Add --run to execute FuXi; no model was loaded.")
        return
    if not pending:
        print("Nothing runnable to do.")
        return
    if args.max_runs < 1 or len(pending) > args.max_runs:
        parser.error(f"{len(pending)} pending runs exceed --max-runs {args.max_runs}")

    handles = prepare_cuda_libraries()
    session = make_session(args.model_dir)
    for day, pressure_zip in pending:
        row = forecast_pair(day, pressure_zip, args, session, observations)
        append_pair(args.output, row)
        print(
            f"Saved pair {row['init_time_utc']} -> {row['valid_time_utc']}: "
            f"FuXi T2M={row['fuxi_t2m_K']:.3f} K, ERA5={row['era5_t2m_K']:.3f} K"
        )
    assert handles is not None  # Keep Windows DLL search-directory handles alive.
    print(f"READY: {args.output}")


if __name__ == "__main__":
    main()

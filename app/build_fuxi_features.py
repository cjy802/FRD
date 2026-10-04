"""Build feature-rich FuXi/ERA5 point pairs from one monthly batch.

This companion to ``build_fuxi_pairs.py`` keeps the original point-pair
workflow intact while saving information that is useful for post-processing:

* analysis tendencies between the two FuXi input frames;
* 3x3 or 5x5 neighbourhood statistics and horizontal gradients;
* FuXi pressure-level values at 925/850/700 hPa;
* 925--700 hPa wind shear;
* configurable six-hourly lead times and initialization hours.

The script extracts small feature vectors from the global arrays in memory and
does not write global forecast NetCDF files.  Initialization hour 00:00 is
intentionally excluded by default because its previous-day input frame is in a
different monthly pressure archive; use 06/12/18 by default or add explicit
previous-day handling before enabling 00:00 in a backfill.
"""

from __future__ import annotations

import argparse
import csv
import gc
import re
import shutil
import tempfile
import zipfile
from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import onnxruntime as ort
import pandas as pd
import xarray as xr

from build_fuxi_pairs import (
    FIELD_MAP,
    PRESSURE_ZIP_NAME,
    CHANNELS,
    discover_pressure_zips,
    extract_zip_nc,
    load_observations,
    make_session,
    time_embedding,
)
from prepare_fuxi1_input import build_input
from run_fuxi1_short import prepare_cuda_libraries


SURFACE_CHANNELS = {
    "t2m_K": "T2M",
    "u10_m_s": "U10",
    "v10_m_s": "V10",
    "msl_Pa": "MSL",
    "tp_6h_mm": "TP",
}
PRESSURE_LEVELS = (925, 850, 700)
PRESSURE_VARS = ("Z", "T", "U", "V", "R")
DEFAULT_INIT_HOURS = (6, 12, 18)
DEFAULT_LEAD_HOURS = (6, 12, 24)


def channel_index(name: str) -> int:
    try:
        return CHANNELS.index(name)
    except ValueError as exc:
        raise ValueError(f"FuXi channel is unavailable: {name}") from exc


def zip_map(folder: Path) -> dict[date, Path]:
    result: dict[date, Path] = {}
    for path in sorted(folder.iterdir()):
        if not path.is_file():
            continue
        match = PRESSURE_ZIP_NAME.fullmatch(path.name)
        if match:
            result[datetime.strptime(match.group(1), "%Y%m%d").date()] = path
    if not result:
        raise FileNotFoundError(f"No pressure ZIPs found in {folder}")
    return result


def existing_keys(path: Path) -> set[tuple[str, int]]:
    if not path.is_file():
        return set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"init_time_utc", "lead_hours"}
        if not required.issubset(reader.fieldnames or ()):
            raise ValueError(f"Feature CSV is missing key columns: {path}")
        return {
            (row["init_time_utc"], int(row["lead_hours"]))
            for row in reader
        }


def point_indices(
    prepared: xr.DataArray,
    latitude: float,
    longitude: float,
) -> tuple[int, int]:
    latitudes = np.asarray(prepared.lat.values)
    longitudes = np.asarray(prepared.lon.values)
    lat_index = int(np.argmin(np.abs(latitudes - latitude)))
    lon_index = int(np.argmin(np.abs(longitudes - longitude)))
    if not (
        np.isclose(latitudes[lat_index], latitude)
        and np.isclose(longitudes[lon_index], longitude)
    ):
        raise ValueError("Requested point is not on the FuXi input grid")
    return lat_index, lon_index


def neighborhood(
    field: np.ndarray,
    lat_index: int,
    lon_index: int,
    size: int,
) -> tuple[float, float, float, float, float]:
    """Return mean, std, north-south and east-west gradients for one field."""
    half = size // 2
    height, width = field.shape
    rows = np.arange(max(0, lat_index - half), min(height, lat_index + half + 1))
    cols = (np.arange(lon_index - half, lon_index + half + 1) % width).astype(int)
    window = field[np.ix_(rows, cols)]
    center = float(field[lat_index, lon_index])
    if not np.isfinite(window).all():
        raise ValueError("FuXi neighbourhood contains non-finite values")
    north_south = 0.0
    east_west = 0.0
    if 0 < lat_index < height - 1:
        north_south = float(
            (field[lat_index - 1, lon_index] - field[lat_index + 1, lon_index]) / 2.0
        )
    east_west = float(
        (field[lat_index, (lon_index + 1) % width]
         - field[lat_index, (lon_index - 1) % width]) / 2.0
    )
    # The center value is deliberately returned separately so callers can
    # make the feature names explicit and inspect the raw point forecast.
    return center, float(window.mean()), float(window.std()), north_south, east_west


def feature_vector(
    state: np.ndarray,
    input_delta: dict[str, float],
    lat_index: int,
    lon_index: int,
    neighborhood_size: int,
) -> dict[str, float]:
    row = dict(input_delta)
    for field, channel in SURFACE_CHANNELS.items():
        values = neighborhood(state[channel_index(channel)], lat_index, lon_index, neighborhood_size)
        prefix = f"feature_nb_{field}"
        row[f"{prefix}_center"] = values[0]
        row[f"{prefix}_mean"] = values[1]
        row[f"{prefix}_std"] = values[2]
        row[f"{prefix}_gradient_ns"] = values[3]
        row[f"{prefix}_gradient_ew"] = values[4]

    for variable in PRESSURE_VARS:
        for level in PRESSURE_LEVELS:
            name = f"{variable}{level}"
            values = neighborhood(
                state[channel_index(name)], lat_index, lon_index, neighborhood_size
            )
            row[f"feature_fuxi_{name}"] = values[0]
            row[f"feature_gradient_ns_{name}"] = values[3]
            row[f"feature_gradient_ew_{name}"] = values[4]

    u925 = state[channel_index("U925"), lat_index, lon_index]
    v925 = state[channel_index("V925"), lat_index, lon_index]
    u700 = state[channel_index("U700"), lat_index, lon_index]
    v700 = state[channel_index("V700"), lat_index, lon_index]
    row["feature_wind_shear_925_700_m_s"] = float(
        np.hypot(u925 - u700, v925 - v700)
    )
    return row


def prepare_input_for_init(
    day: date,
    init_hour: int,
    pressure_zip: Path,
    args: argparse.Namespace,
    work_dir: Path,
) -> Path:
    if init_hour == 0:
        raise ValueError(
            "init hour 00 requires the previous day's pressure/surface frame; "
            "use --init-hours 6 12 18 for the monthly backfill"
        )
    init_time = datetime(day.year, day.month, day.day, init_hour)
    candidate = (
        args.prepared_dir / f"input_{day:%Y%m%d}_{init_hour:02d}.nc"
        if args.prepared_dir else None
    )
    if candidate is not None and candidate.is_file():
        return candidate
    pressure_nc = work_dir / f"pressure_{init_hour:02d}.nc"
    prepared_nc = work_dir / f"fuxi_input_{init_hour:02d}.nc"
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


def load_prepared(path: Path, latitude: float, longitude: float) -> tuple[np.ndarray, int, int]:
    with xr.open_dataarray(path) as prepared:
        if prepared.shape != (2, 70, 721, 1440):
            raise ValueError(f"Invalid FuXi input shape: {prepared.shape}")
        if tuple(str(value) for value in prepared.level.values) != CHANNELS:
            raise ValueError("FuXi channel order is incorrect")
        lat_index, lon_index = point_indices(prepared, latitude, longitude)
        return np.asarray(prepared.values[None], dtype=np.float32), lat_index, lon_index


def input_tendencies(input_values: np.ndarray, lat_index: int, lon_index: int) -> dict[str, float]:
    row: dict[str, float] = {}
    for field, channel in SURFACE_CHANNELS.items():
        row[f"feature_analysis_delta_6h_{field}"] = float(
            input_values[0, 1, channel_index(channel), lat_index, lon_index]
            - input_values[0, 0, channel_index(channel), lat_index, lon_index]
        )
    return row


def make_rows_for_day(
    day: date,
    pressure_zip: Path,
    args: argparse.Namespace,
    session: ort.InferenceSession,
    observations: pd.DataFrame,
    completed: set[tuple[str, int]],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    scratch = (
        nullcontext(args.prepared_dir)
        if args.prepared_dir is not None
        else tempfile.TemporaryDirectory(prefix="fuxi_features_")
    )
    with scratch as location:
        work_dir = location if isinstance(location, Path) else Path(location)
        for init_hour in args.init_hours:
            init_time = datetime(day.year, day.month, day.day, init_hour, tzinfo=timezone.utc)
            try:
                input_path = prepare_input_for_init(day, init_hour, pressure_zip, args, work_dir)
                input_values, lat_index, lon_index = load_prepared(
                    input_path, args.latitude, args.longitude
                )
            except ValueError as exc:
                print(f"BLOCKED {day} {init_hour:02d}:00: {exc}")
                continue

            state = input_values
            input_delta = input_tendencies(input_values, lat_index, lon_index)
            max_steps = max(args.lead_hours) // 6
            for step in range(1, max_steps + 1):
                step_init = init_time + timedelta(hours=(step - 1) * 6)
                output_values = session.run(
                    None,
                    {"input": state, "temb": time_embedding(step_init.replace(tzinfo=None))},
                )[0]
                if output_values.shape != (1, 2, 70, 721, 1440):
                    raise ValueError(f"Unexpected FuXi output shape: {output_values.shape}")
                forecast = np.asarray(output_values[0, -1], dtype=np.float32)
                valid_time = init_time + timedelta(hours=step * 6)
                lead = step * 6
                if lead in args.lead_hours:
                    key = (init_time.strftime("%Y-%m-%dT%H:%M:%SZ"), lead)
                    if key not in completed:
                        if valid_time not in observations.index:
                            print(f"BLOCKED {valid_time.isoformat()}: missing ERA5 observation")
                        else:
                            obs = observations.loc[valid_time]
                            row: dict[str, object] = {
                                "init_time_utc": key[0],
                                "valid_time_utc": valid_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                "lead_hours": lead,
                                "latitude_deg": args.latitude,
                                "longitude_deg": args.longitude,
                                "model": "FuXi-1.0-short",
                                "pressure_zip": pressure_zip.name,
                            }
                            for field, channel, observation_column in FIELD_MAP:
                                forecast_value = float(forecast[channel_index(channel), lat_index, lon_index])
                                observed = float(obs[observation_column])
                                row[f"fuxi_{field}"] = forecast_value
                                row[f"era5_{field}"] = observed
                                row[f"residual_{field}"] = observed - forecast_value
                            row.update(
                                feature_vector(
                                    forecast,
                                    input_delta,
                                    lat_index,
                                    lon_index,
                                    args.neighborhood_size,
                                )
                            )
                            rows.append(row)
                state = np.stack((state[:, 1], output_values[:, -1]), axis=1)
                del output_values, forecast
                gc.collect()
            del state, input_values
            gc.collect()
    return rows


def append_rows(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0])
    if path.exists():
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            existing_fields = list(csv.reader(handle).__next__())
        if existing_fields != fields:
            raise ValueError("Existing feature CSV schema differs from this run")
        mode = "a"
    else:
        mode = "w"
    with path.open(mode, encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if mode == "w":
            writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pressure-dir", type=Path, required=True)
    parser.add_argument("--surface", type=Path, required=True)
    parser.add_argument("--wind", type=Path, required=True)
    parser.add_argument("--precip", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prepared-dir", type=Path)
    parser.add_argument("--date", action="append")
    parser.add_argument("--latitude", type=float, default=31.5)
    parser.add_argument("--longitude", type=float, default=119.5)
    parser.add_argument("--init-hours", type=int, nargs="+", default=list(DEFAULT_INIT_HOURS))
    parser.add_argument("--lead-hours", type=int, nargs="+", default=list(DEFAULT_LEAD_HOURS))
    parser.add_argument("--neighborhood-size", type=int, choices=(3, 5), default=3)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--max-runs", type=int, default=1)
    args = parser.parse_args()
    args.init_hours = sorted(set(args.init_hours))
    args.lead_hours = sorted(set(args.lead_hours))
    if not args.init_hours or any(hour not in (0, 6, 12, 18) for hour in args.init_hours):
        parser.error("init-hours must be chosen from 0, 6, 12, 18")
    if not args.lead_hours or any(lead < 6 or lead % 6 for lead in args.lead_hours):
        parser.error("lead-hours must be positive multiples of 6")
    dates = {date.fromisoformat(text) for text in (args.date or [])}
    pressure = zip_map(args.pressure_dir)
    selected = sorted(dates or pressure.keys())
    selected = [day for day in selected if day in pressure]
    if not selected:
        raise FileNotFoundError("No selected pressure dates are available")
    observations = load_observations(args.observations, args.latitude, args.longitude)
    completed = existing_keys(args.output)
    print(
        f"Dates: {len(selected)}; init hours: {args.init_hours}; "
        f"lead hours: {args.lead_hours}; neighbourhood: {args.neighborhood_size}x{args.neighborhood_size}"
    )
    if not args.run:
        print("PREVIEW ONLY. Add --run to infer and save feature rows.")
        return 0
    expected_runs = len(selected) * len(args.init_hours)
    if args.max_runs < 1 or expected_runs > args.max_runs:
        parser.error(f"{expected_runs} date/init runs exceed --max-runs {args.max_runs}")
    handles = prepare_cuda_libraries()
    session = make_session(args.model_dir)
    for day in selected:
        rows = make_rows_for_day(day, pressure[day], args, session, observations, completed)
        append_rows(args.output, rows)
        completed.update((str(row["init_time_utc"]), int(row["lead_hours"])) for row in rows)
        print(f"Saved {len(rows)} feature rows for {day}: {args.output}")
    assert handles is not None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

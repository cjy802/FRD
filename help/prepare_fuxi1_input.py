"""Build a FuXi-1.0 input from CDS ERA5 global NetCDF files.

The output is two six-hourly frames with the exact 70-channel order expected by
the original FuXi ONNX model. This script never modifies the source datasets.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path

import netCDF4 as nc
import numpy as np
import xarray as xr


LEVELS = (50, 100, 150, 200, 250, 300, 400, 500, 600, 700, 850, 925, 1000)
PRESSURE_VARIABLES = ("z", "t", "u", "v", "r")
SURFACE_VARIABLES = ("t2m", "u10", "v10", "msl", "tp")
CHANNELS = tuple(f"{name.upper()}{level}" for name in PRESSURE_VARIABLES for level in LEVELS)
CHANNELS += tuple(name.upper() for name in SURFACE_VARIABLES)
EXPECTED_GRID = (721, 1440)


def time_index(dataset: nc.Dataset, when: datetime) -> int:
    coordinate = dataset.variables["valid_time"]
    numeric = nc.date2num(
        when,
        units=coordinate.units,
        calendar=getattr(coordinate, "calendar", "standard"),
    )
    matches = np.flatnonzero(np.asarray(coordinate[:]) == numeric)
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one ERA5 field at {when.isoformat()}, found {len(matches)}")
    return int(matches[0])


def read_grid(variable: nc.Variable, indices: tuple[int, ...]) -> np.ndarray:
    values = np.asarray(np.ma.filled(variable[indices], np.nan), dtype=np.float32)
    if values.shape != EXPECTED_GRID:
        raise ValueError(f"{variable.name} has shape {values.shape}; expected {EXPECTED_GRID}")
    if not np.isfinite(values).all():
        raise ValueError(f"{variable.name} contains missing or non-finite values")
    return values


def check_coordinates(datasets: dict[str, nc.Dataset]) -> tuple[np.ndarray, np.ndarray]:
    reference = datasets["pressure"]
    latitude = np.asarray(reference.variables["latitude"][:])
    longitude = np.asarray(reference.variables["longitude"][:])
    if (len(latitude), len(longitude)) != EXPECTED_GRID:
        raise ValueError("Pressure-level data is not the required 721 x 1440 global grid")
    if not (np.isclose(latitude[0], 90) and np.isclose(latitude[-1], -90)):
        raise ValueError("Latitude must run from 90 to -90 degrees")
    if not (np.isclose(longitude[0], 0) and np.isclose(longitude[-1], 359.75)):
        raise ValueError("Longitude must run from 0 to 359.75 degrees")
    for kind, dataset in datasets.items():
        if not np.array_equal(latitude, dataset.variables["latitude"][:]):
            raise ValueError(f"Latitude grid in {kind} does not match pressure-level data")
        if not np.array_equal(longitude, dataset.variables["longitude"][:]):
            raise ValueError(f"Longitude grid in {kind} does not match pressure-level data")
    return latitude, longitude


def build_input(args: argparse.Namespace) -> None:
    if args.output.exists():
        raise FileExistsError(f"Refusing to overwrite existing input: {args.output}")
    init_time = datetime.strptime(args.init_time, "%Y-%m-%dT%H:%M")
    frames = (init_time - timedelta(hours=6), init_time)

    with ExitStack() as stack:
        datasets = {
            "pressure": stack.enter_context(nc.Dataset(args.pressure, "r")),
            "surface": stack.enter_context(nc.Dataset(args.surface, "r")),
            "wind": stack.enter_context(nc.Dataset(args.wind, "r")),
            "precip": stack.enter_context(nc.Dataset(args.precip, "r")),
        }
        latitude, longitude = check_coordinates(datasets)

        pressure = datasets["pressure"]
        available_levels = np.asarray(pressure.variables["pressure_level"][:])
        if len(available_levels) != len(LEVELS) or set(available_levels.tolist()) != set(LEVELS):
            raise ValueError(f"Pressure levels do not match FuXi's 13 levels: {available_levels.tolist()}")
        level_index = {int(level): index for index, level in enumerate(available_levels)}

        # Check all required timestamps before creating a potentially large file.
        for when in frames:
            for kind in datasets:
                time_index(datasets[kind], when)
            for hours_back in range(5, -1, -1):
                time_index(datasets["precip"], when - timedelta(hours=hours_back))

        args.output.parent.mkdir(parents=True, exist_ok=True)
        with nc.Dataset(args.output, "w", format="NETCDF4") as output:
            output.createDimension("time", 2)
            output.createDimension("level", len(CHANNELS))
            output.createDimension("lat", len(latitude))
            output.createDimension("lon", len(longitude))

            times = output.createVariable("time", "i8", ("time",))
            times.units = "seconds since 1970-01-01 00:00:00"
            times.calendar = "proleptic_gregorian"
            times[:] = nc.date2num(frames, units=times.units, calendar=times.calendar)
            labels = output.createVariable("level", str, ("level",))
            labels[:] = np.asarray(CHANNELS, dtype=object)
            lats = output.createVariable("lat", "f8", ("lat",))
            lons = output.createVariable("lon", "f8", ("lon",))
            lats[:] = latitude
            lons[:] = longitude

            field = output.createVariable(
                "data", "f4", ("time", "level", "lat", "lon"),
                zlib=True, complevel=1, chunksizes=(1, 1, len(latitude), len(longitude)),
            )
            output.setncattr("description", "FuXi-1.0 70-channel ERA5 input")
            output.setncattr("init_time_utc", init_time.isoformat())
            output.setncattr("precipitation", "sum of six hourly ERA5 tp fields, converted m to mm, clipped 0..1000")

            for frame_index, when in enumerate(frames):
                pressure_time = time_index(pressure, when)
                surface_time = time_index(datasets["surface"], when)
                wind_time = time_index(datasets["wind"], when)
                channel_index = 0

                for name in PRESSURE_VARIABLES:
                    variable = pressure.variables[name]
                    for level in LEVELS:
                        grid = read_grid(variable, (pressure_time, level_index[level], slice(None), slice(None)))
                        field[frame_index, channel_index, :, :] = grid
                        channel_index += 1

                for name in SURFACE_VARIABLES[:-1]:
                    source = datasets["wind"] if name in ("u10", "v10") else datasets["surface"]
                    source_time = wind_time if name in ("u10", "v10") else surface_time
                    grid = read_grid(source.variables[name], (source_time, slice(None), slice(None)))
                    field[frame_index, channel_index, :, :] = grid
                    channel_index += 1

                precipitation = np.zeros(EXPECTED_GRID, dtype=np.float64)
                for hours_back in range(5, -1, -1):
                    hour = when - timedelta(hours=hours_back)
                    index = time_index(datasets["precip"], hour)
                    precipitation += read_grid(
                        datasets["precip"].variables["tp"],
                        (index, slice(None), slice(None)),
                    )
                precipitation = np.clip(precipitation * 1000.0, 0.0, 1000.0).astype(np.float32)
                field[frame_index, channel_index, :, :] = precipitation
                print(f"Prepared {when.isoformat()} UTC: {channel_index + 1} channels")

    # The original FuXi script uses xarray.open_dataarray, so verify that exact reader.
    with xr.open_dataarray(args.output) as prepared:
        if prepared.shape != (2, 70, *EXPECTED_GRID):
            raise ValueError(f"Output shape is wrong: {prepared.shape}")
        if tuple(str(value) for value in prepared.level.values) != CHANNELS:
            raise ValueError("Output channel order is wrong")
        if not np.array_equal(
            prepared.time.values,
            np.asarray(frames, dtype="datetime64[ns]"),
        ):
            raise ValueError("Output time coordinates are wrong")
        point = prepared.sel(lat=31.5, lon=119.5)
        print("Point check (31.5N, 119.5E):")
        for name in ("T2M", "U10", "V10", "MSL", "TP"):
            print(f"  {name}: {point.sel(level=name).values.tolist()}")
    print(f"READY: {args.output} ({args.output.stat().st_size:,} bytes)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pressure", type=Path, required=True)
    parser.add_argument("--surface", type=Path, required=True, help="file with t2m and msl")
    parser.add_argument("--wind", type=Path, required=True, help="file with u10 and v10")
    parser.add_argument("--precip", type=Path, required=True, help="file with hourly tp")
    parser.add_argument("--init-time", default="2026-01-15T06:00", help="UTC initialization YYYY-MM-DDTHH:MM")
    parser.add_argument("--output", type=Path, required=True)
    build_input(parser.parse_args())


if __name__ == "__main__":
    main()

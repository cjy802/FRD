"""Extract one ERA5 grid point from four global CDS NetCDF downloads.

This produces two compact CSV files without changing the global source data:
one hourly surface series and one pressure-level table.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import netCDF4 as nc
import numpy as np
import pandas as pd
import xarray as xr


SURFACE_COLUMNS = (
    "valid_time_utc", "latitude_deg", "longitude_deg", "t2m_K", "t2m_C",
    "msl_Pa", "msl_hPa", "u10_m_s", "v10_m_s", "ws10_m_s",
    "tp_hourly_m", "tp_hourly_mm", "tp_6h_mm",
)
PRESSURE_COLUMNS = (
    "valid_time_utc", "latitude_deg", "longitude_deg", "pressure_hPa",
    "z_m2_s2", "r_percent", "t_K", "u_m_s", "v_m_s",
)


def find_point(dataset: nc.Dataset, latitude: float, longitude: float) -> tuple[int, int]:
    latitudes = np.asarray(dataset.variables["latitude"][:], dtype=np.float64)
    longitudes = np.asarray(dataset.variables["longitude"][:], dtype=np.float64)
    lat_index = int(np.argmin(np.abs(latitudes - latitude)))
    lon_index = int(np.argmin(np.abs(longitudes - longitude)))
    actual = (float(latitudes[lat_index]), float(longitudes[lon_index]))
    if not (np.isclose(actual[0], latitude) and np.isclose(actual[1], longitude)):
        raise ValueError(f"Requested grid point {(latitude, longitude)} is unavailable; nearest is {actual}")
    return lat_index, lon_index


def times(dataset: nc.Dataset) -> pd.DatetimeIndex:
    coordinate = dataset.variables["valid_time"]
    if not str(coordinate.units).startswith("seconds since 1970-01-01"):
        raise ValueError(f"Unexpected time units: {coordinate.units}")
    values = np.asarray(coordinate[:], dtype=np.int64)
    if len(values) != len(np.unique(values)) or np.any(np.diff(values) <= 0):
        raise ValueError("valid_time contains duplicates or is not increasing")
    return pd.to_datetime(values, unit="s", utc=True)


def point_values(variable: nc.Variable, lat_index: int, lon_index: int) -> np.ndarray:
    values = variable[:, lat_index, lon_index]
    return np.asarray(np.ma.filled(values, np.nan), dtype=np.float64)


def read_surface(
    surface: nc.Dataset,
    wind: nc.Dataset,
    precipitation: nc.Dataset,
    latitude: float,
    longitude: float,
) -> pd.DataFrame:
    pieces: list[pd.DataFrame] = []
    for dataset, names in (
        (surface, ("t2m", "msl")),
        (wind, ("u10", "v10")),
        (precipitation, ("tp",)),
    ):
        lat_index, lon_index = find_point(dataset, latitude, longitude)
        index = times(dataset)
        piece = pd.DataFrame(index=index)
        for name in names:
            piece[name] = point_values(dataset.variables[name], lat_index, lon_index)
        pieces.append(piece)

    result = pieces[0].join(pieces[1], how="outer").join(pieces[2], how="outer")
    result = result.sort_index()
    result.index.name = "valid_time_utc"
    result["latitude_deg"] = latitude
    result["longitude_deg"] = longitude
    result["t2m_K"] = result["t2m"]
    result["t2m_C"] = result["t2m"] - 273.15
    result["msl_Pa"] = result["msl"]
    result["msl_hPa"] = result["msl"] / 100.0
    result["u10_m_s"] = result["u10"]
    result["v10_m_s"] = result["v10"]
    result["ws10_m_s"] = np.hypot(result["u10"], result["v10"])
    result["tp_hourly_m"] = result["tp"]
    result["tp_hourly_mm"] = result["tp"] * 1000.0

    # ERA5's hourly accumulated tp ends at its valid_time. Do not silently
    # bridge a missing timestamp when computing the preceding six-hour total.
    hourly = result["tp_hourly_mm"]
    consecutive = result.index.to_series().diff().eq(pd.Timedelta(hours=1))
    six_consecutive = consecutive.rolling(5, min_periods=5).sum().eq(5)
    result["tp_6h_mm"] = hourly.rolling(6, min_periods=6).sum().where(six_consecutive)

    output = result.reset_index()
    output["valid_time_utc"] = output["valid_time_utc"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return output.loc[:, list(SURFACE_COLUMNS)]


def read_pressure(dataset: nc.Dataset, latitude: float, longitude: float) -> pd.DataFrame:
    lat_index, lon_index = find_point(dataset, latitude, longitude)
    index = times(dataset)
    levels = np.asarray(dataset.variables["pressure_level"][:], dtype=np.float64)
    if len(levels) != len(np.unique(levels)):
        raise ValueError("pressure_level contains duplicates")
    values = {
        name: np.asarray(
            np.ma.filled(dataset.variables[name][:, :, lat_index, lon_index], np.nan),
            dtype=np.float64,
        )
        for name in ("z", "r", "t", "u", "v")
    }
    rows = []
    for time_index, timestamp in enumerate(index):
        for level_index in np.argsort(levels):
            rows.append(
                (
                    timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    latitude,
                    longitude,
                    float(levels[level_index]),
                    *(float(values[name][time_index, level_index]) for name in ("z", "r", "t", "u", "v")),
                )
            )
    return pd.DataFrame(rows, columns=PRESSURE_COLUMNS)


def compare_reference(surface: pd.DataFrame, reference_path: Path) -> None:
    with xr.open_dataset(reference_path) as reference:
        if not (
            np.isclose(float(reference.latitude.values), float(surface.latitude_deg.iloc[0]))
            and np.isclose(float(reference.longitude.values), float(surface.longitude_deg.iloc[0]))
        ):
            raise ValueError("Reference point coordinates do not match requested location")
        expected = reference[["t2m", "msl", "u10", "v10", "tp"]].to_dataframe()
    expected.index = pd.to_datetime(expected.index, utc=True)
    actual = surface.set_index(pd.to_datetime(surface.valid_time_utc, utc=True))
    common = actual.index.intersection(expected.index)
    print(f"Reference overlap: {len(common)} timestamps")
    for column, source_name in (
        ("t2m_K", "t2m"), ("msl_Pa", "msl"),
        ("u10_m_s", "u10"), ("v10_m_s", "v10"),
        ("tp_hourly_m", "tp"),
    ):
        left = actual.loc[common, column].to_numpy(dtype=np.float64)
        right = expected.loc[common, source_name].to_numpy(dtype=np.float64)
        finite = np.isfinite(left) & np.isfinite(right)
        difference = np.abs(left[finite] - right[finite])
        print(
            f"  {column}: compared={len(difference)}, "
            f"mean_abs_diff={difference.mean():.8g}, max_abs_diff={difference.max():.8g}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--surface", type=Path, required=True, help="global t2m/msl NetCDF")
    parser.add_argument("--wind", type=Path, required=True, help="global u10/v10 NetCDF")
    parser.add_argument("--precip", type=Path, required=True, help="global hourly tp NetCDF")
    parser.add_argument("--pressure", type=Path, required=True, help="global pressure-level NetCDF")
    parser.add_argument("--reference", type=Path, help="optional existing point ERA5 NetCDF for cross-check")
    parser.add_argument("--latitude", type=float, default=31.5)
    parser.add_argument("--longitude", type=float, default=119.5)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    surface_target = args.output_dir / "era5_surface_point_31p5N_119p5E_2026.csv"
    pressure_target = args.output_dir / "era5_pressure_point_31p5N_119p5E_20260115.csv"
    for target in (surface_target, pressure_target):
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite existing output: {target}")

    with nc.Dataset(args.surface) as surface_source, nc.Dataset(args.wind) as wind_source, \
            nc.Dataset(args.precip) as precip_source, nc.Dataset(args.pressure) as pressure_source:
        surface_table = read_surface(
            surface_source, wind_source, precip_source, args.latitude, args.longitude
        )
        pressure_table = read_pressure(pressure_source, args.latitude, args.longitude)

    print(
        f"Surface: {len(surface_table)} rows, "
        f"{surface_table.valid_time_utc.iloc[0]} to {surface_table.valid_time_utc.iloc[-1]}"
    )
    print("Surface missing:", surface_table.isna().sum().to_dict())
    print(f"Pressure: {len(pressure_table)} rows, {pressure_table.pressure_hPa.nunique()} levels")
    print("Pressure missing:", pressure_table.isna().sum().to_dict())
    if args.reference:
        compare_reference(surface_table, args.reference)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    surface_table.to_csv(surface_target, index=False, encoding="utf-8-sig", float_format="%.9g")
    pressure_table.to_csv(pressure_target, index=False, encoding="utf-8-sig", float_format="%.9g")
    print(f"Saved {surface_target} ({surface_target.stat().st_size:,} bytes)")
    print(f"Saved {pressure_target} ({pressure_target.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()

"""Inspect a prepared current global input or run one FuXi +6h point forecast.

stdout contains one JSON document; initialization and CUDA logs go to stderr.
No observations, historical pair CSV, or global forecast output are needed.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from datetime import timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

from build_fuxi_pairs import FIELD_MAP, make_session, time_embedding
from prepare_fuxi1_input import CHANNELS
from run_fuxi1_short import prepare_cuda_libraries


def inspect_input(path: Path, latitude: float, longitude: float) -> tuple[dict, int, int]:
    with xr.open_dataarray(path) as data:
        if data.shape != (2, 70, 721, 1440):
            raise ValueError(f"Expected prepared shape (2,70,721,1440), got {data.shape}")
        if tuple(str(value) for value in data.level.values) != CHANNELS:
            raise ValueError("Prepared input has the wrong 70-channel order")
        if not np.all(np.diff(data.time.values) == np.timedelta64(6, "h")):
            raise ValueError("Prepared input frames must be six hours apart")
        latitudes, longitudes = np.asarray(data.lat.values), np.asarray(data.lon.values)
        if not (np.isclose(latitudes[0], 90) and np.isclose(latitudes[-1], -90)
                and np.isclose(longitudes[0], 0) and np.isclose(longitudes[-1], 359.75)):
            raise ValueError("Prepared input must use the original FuXi global grid")
        lat_index = int(np.argmin(abs(latitudes - latitude)))
        lon_index = int(np.argmin(abs(longitudes - longitude)))
        if not (np.isclose(latitudes[lat_index], latitude) and np.isclose(longitudes[lon_index], longitude)):
            raise ValueError("Requested point is not on the input grid")
        stamps = [pd.Timestamp(value).to_pydatetime().replace(tzinfo=timezone.utc) for value in data.time.values]
        metadata = {
            "previous_time_utc": stamps[0].isoformat(), "init_time_utc": stamps[1].isoformat(),
            "valid_time_utc": (stamps[1] + timedelta(hours=6)).isoformat(),
            "lead_hours": 6, "latitude_deg": latitude, "longitude_deg": longitude,
        }
        return metadata, lat_index, lon_index


def predict_point(args: argparse.Namespace, metadata: dict, lat_index: int, lon_index: int) -> dict:
    with xr.open_dataarray(args.input) as data:
        values = np.asarray(data.values[None], dtype=np.float32)
    if not np.isfinite(values).all():
        raise ValueError("Prepared input contains non-finite values")
    with contextlib.redirect_stdout(sys.stderr):
        handles = prepare_cuda_libraries()
        session = make_session(args.model_dir)
    init = pd.Timestamp(metadata["init_time_utc"]).to_pydatetime().replace(tzinfo=None)
    output = session.run(None, {"input": values, "temb": time_embedding(init)})[0]
    if output.shape != (1, 2, 70, 721, 1440):
        raise ValueError(f"Unexpected FuXi output shape: {output.shape}")
    point = output[0, -1, :, lat_index, lon_index]
    if not np.isfinite(point).all():
        raise ValueError("FuXi point output contains non-finite values")
    assert handles is not None
    return {**metadata, "raw": {field: float(point[CHANNELS.index(channel)]) for field, channel, _ in FIELD_MAP}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--latitude", type=float, default=31.5)
    parser.add_argument("--longitude", type=float, default=119.5)
    parser.add_argument("--inspect", action="store_true")
    args = parser.parse_args()
    try:
        metadata, lat_index, lon_index = inspect_input(args.input, args.latitude, args.longitude)
        if not args.inspect and args.model_dir is None:
            parser.error("--model-dir is required for prediction")
        result = metadata if args.inspect else predict_point(args, metadata, lat_index, lon_index)
        print(json.dumps(result, allow_nan=False))
        return 0
    except Exception as error:
        print(json.dumps({"error": f"{type(error).__name__}: {error}"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

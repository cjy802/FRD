"""Keep a small native FuXi region, never a fabricated high-resolution grid."""

import math

import numpy as np

from prepare_fuxi1_input import CHANNELS


DEFAULT_BOUNDS = (116.0, 30.0, 122.5, 35.5)  # west, south, east, north


def crop_region(state: np.ndarray, valid_time: str, lead_hours: int,
                bounds=DEFAULT_BOUNDS) -> dict:
    west, south, east, north = bounds
    if not (0 <= west < east < 360 and -90 <= south < north <= 90):
        raise ValueError("Invalid non-wrapping regional bounds")
    if state.shape != (70, 721, 1440):
        raise ValueError("Regional extraction requires the original FuXi grid")
    i0, i1 = math.floor((90 - north) * 4), math.ceil((90 - south) * 4)
    j0, j1 = math.floor(west * 4), math.ceil(east * 4)
    values = state[:, i0:i1 + 1, j0:j1 + 1]
    if not np.isfinite(values).all():
        raise ValueError("Region contains non-finite values")
    return {
        "latitudes": (90 - np.arange(i0, i1 + 1) * .25).tolist(),
        "longitudes": (np.arange(j0, j1 + 1) * .25).tolist(),
        "channels": list(CHANNELS), "native_resolution_deg": .25,
        "snapshots": [{"valid_time_utc": valid_time, "lead_hours": lead_hours,
                       "values": values.tolist()}],
    }

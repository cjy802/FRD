"""Automatically prepare current FuXi input from NOAA's public GFS files.

Fetch only the 69 state fields and two genuine six-hour precipitation fields,
using GRIB inventories and validated HTTP byte ranges. No API key is needed.
Use analysis where released, otherwise an explicitly recorded short-range GFS
background valid at the same time. Output follows the existing 70-channel
contract, including geopotential (not height) and rainfall in mm.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import netCDF4 as nc
import numpy as np
import requests

from prepare_fuxi1_input import CHANNELS, LEVELS


APP_ROOT = Path(__file__).resolve().parent
BASE_URL = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
GRID = (721, 1440)
VARIABLES = {"HGT": "Z", "TMP": "T", "UGRD": "U", "VGRD": "V", "RH": "R"}
SURFACE = {("TMP", "2 m above ground"): "T2M", ("UGRD", "10 m above ground"): "U10",
           ("VGRD", "10 m above ground"): "V10", ("MSLET", "mean sea level"): "MSL"}


def six_hour_time(now: datetime) -> datetime:
    return now.replace(hour=now.hour // 6 * 6, minute=0, second=0, microsecond=0)


def source_url(cycle: datetime, hour: int) -> str:
    return f"{BASE_URL}/gfs.{cycle:%Y%m%d}/{cycle:%H}/atmos/gfs.t{cycle:%H}z.pgrb2.0p25.f{hour:03d}"


def parse_inventory(text: str) -> list[dict]:
    records = []
    for line in text.splitlines():
        fields = line.split(":")
        if len(fields) < 6 or not fields[0].isdigit() or not fields[1].isdigit():
            continue
        records.append({"start": int(fields[1]), "variable": fields[3], "level": fields[4],
                        "period": fields[5], "cycle": fields[2]})
    if not records:
        raise ValueError("GFS inventory is empty or invalid")
    for index, record in enumerate(records):
        record["end"] = records[index + 1]["start"] - 1 if index + 1 < len(records) else None
        if record["end"] is not None and record["end"] < record["start"]:
            raise ValueError("GFS inventory offsets are not increasing")
    return records


def select_records(records: list[dict], kind: str) -> list[dict]:
    wanted = {"TP"} if kind == "rain" else set(CHANNELS) - {"TP"}
    result = {}
    for record in records:
        variable, level = record["variable"], record["level"]
        channel = SURFACE.get((variable, level))
        if variable in VARIABLES and level in {f"{value} mb" for value in LEVELS}:
            channel = f"{VARIABLES[variable]}{level.split()[0]}"
        if variable == "APCP" and level == "surface" and "0-6 hour acc" in record["period"]:
            channel = "TP"
        if channel in wanted:
            if channel in result:
                # GFS can expose two APCP messages for the same 0--6h interval.
                # Keep the first complete six-hour accumulator deterministically.
                if kind == "rain":
                    continue
                raise ValueError(f"Duplicate GFS field {channel}")
            result[channel] = {**record, "channel": channel}
    missing = wanted - result.keys()
    if missing:
        raise ValueError(f"GFS source lacks fields: {sorted(missing)}")
    return sorted(result.values(), key=lambda record: record["start"])


def native_nc_path(path: Path) -> str:
    """Use an existing, verified ASCII junction for this Windows NetCDF library."""
    alias = Path("D:/DATASET/FuXi_RuntimeLinks/FuXiApp")
    try:
        relative = path.resolve().relative_to(APP_ROOT.resolve())
        if alias.is_dir() and alias.resolve() == APP_ROOT.resolve():
            return str(alias / relative)
    except ValueError:
        pass
    return str(path)


class GFSDownloader:
    def __init__(self, cache: Path):
        self.cache = cache
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "LowAltitude-FuXi/1.0 (NOAA public initial fields)"
        self.inventories = {}

    def inventory(self, cycle: datetime, hour: int) -> list[dict]:
        key = (cycle, hour)
        if key not in self.inventories:
            response = self.session.get(source_url(cycle, hour) + ".idx", timeout=(10, 30))
            response.raise_for_status()
            records = parse_inventory(response.text)
            if any(record["cycle"] != f"d={cycle:%Y%m%d%H}" for record in records):
                raise ValueError("GFS inventory cycle does not match request")
            self.inventories[key] = records
        return self.inventories[key]

    def choose_state(self, valid: datetime) -> tuple[datetime, int]:
        errors = []
        for hour in (0, 6, 12):
            cycle = valid - timedelta(hours=hour)
            try:
                select_records(self.inventory(cycle, hour), "state")
                return cycle, hour
            except (requests.RequestException, ValueError) as error:
                errors.append(str(error))
        raise RuntimeError(f"NOAA current background is unavailable: {errors[-1]}")

    def fetch_fields(self, cycle: datetime, hour: int, kind: str) -> Path:
        records = select_records(self.inventory(cycle, hour), kind)
        target = self.cache / f"gfs_{cycle:%Y%m%d%H}_f{hour:03d}_{kind}.grib2"
        manifest = target.with_suffix(".json")
        expected = [{"channel": record["channel"], "start": record["start"], "end": record["end"]} for record in records]
        if target.is_file() and manifest.is_file():
            try:
                info = json.loads(manifest.read_text(encoding="utf-8"))
            except ValueError:
                info = {}
            if info.get("fields") == expected and info.get("bytes") == target.stat().st_size:
                print(f"Reusing GFS fields: {target.name}", file=sys.stderr)
                return target
        self.cache.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(".part")
        count = 0
        groups = []
        for record in records:
            if groups and groups[-1]["end"] is not None and groups[-1]["end"] + 1 == record["start"]:
                groups[-1]["end"] = record["end"]
                groups[-1]["fields"] += 1
            else:
                groups.append({"start": record["start"], "end": record["end"], "fields": 1})
        done = 0
        with partial.open("wb") as handle:
            for record in groups:
                start, end = record["start"], record["end"]
                interval = f"bytes={start}-{end if end is not None else ''}"
                for attempt in range(3):
                    try:
                        with self.session.get(source_url(cycle, hour), headers={"Range": interval},
                                              stream=True, timeout=(10, 60)) as response:
                            response.raise_for_status()
                            if response.status_code != 206:
                                raise ValueError("GFS server ignored the byte range; refusing a full-file download")
                            content_range = response.headers.get("Content-Range", "")
                            if not content_range.startswith(f"bytes {start}-"):
                                raise ValueError("GFS server returned the wrong byte range")
                            payload = bytearray()
                            for chunk in response.iter_content(1024 * 1024):
                                payload.extend(chunk)
                                if len(payload) > 32 * 1024 * 1024:
                                    raise ValueError("GFS message exceeds the expected bound")
                        if end is not None and len(payload) != end - start + 1:
                            raise ValueError("Incomplete GFS byte range")
                        if payload[:4] != b"GRIB" or payload[-4:] != b"7777":
                            raise ValueError("GFS response is not a complete GRIB message")
                        handle.write(payload)
                        count += len(payload)
                        break
                    except (requests.RequestException, ValueError):
                        if attempt == 2:
                            raise
                        time.sleep(2)
                done += record["fields"]
                print(f"GFS {cycle:%m-%d %H} f{hour:03d} {kind}: {done}/{len(records)}", file=sys.stderr)
        partial.replace(target)
        manifest.write_text(json.dumps({"generator": "fuxi-gfs-live", "fields": expected, "bytes": count}), encoding="utf-8")
        return target

    def prune_old_cache(self, now: datetime):
        """Remove only this provider's recorded GRIB cache older than 48h."""
        for manifest in self.cache.glob("gfs_*.json"):
            match = re.fullmatch(r"gfs_(\d{10})_f(\d{3})_(state|rain)\.json", manifest.name)
            if not match:
                continue
            cycle = datetime.strptime(match[1], "%Y%m%d%H").replace(tzinfo=timezone.utc)
            valid = cycle + timedelta(hours=int(match[2]))
            if valid >= now - timedelta(hours=48):
                continue
            try:
                info = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if info.get("generator") != "fuxi-gfs-live":
                continue
            target = manifest.with_suffix(".grib2")
            if target.resolve().parent == self.cache.resolve() and manifest.resolve().parent == self.cache.resolve():
                target.unlink(missing_ok=True)
                manifest.unlink()


def grib_arrays(path: Path, valid: datetime, kind: str):
    import eccodes as ec
    with path.open("rb") as handle:
        while True:
            header = handle.read(16)
            if not header:
                break
            if len(header) != 16 or header[:4] != b"GRIB" or header[7] != 2:
                raise ValueError("Invalid cached GRIB2 header")
            length = int.from_bytes(header[8:16], "big")
            if not 20 <= length <= 32 * 1024 * 1024:
                raise ValueError("Invalid GRIB message size")
            message = header + handle.read(length - 16)
            if len(message) != length:
                raise ValueError("Truncated cached GRIB message")
            gid = ec.codes_new_from_message(message)
            try:
                stamp = datetime.strptime(f"{ec.codes_get(gid, 'validityDate'):08d}{ec.codes_get(gid, 'validityTime'):04d}", "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
                if stamp != valid:
                    raise ValueError("GFS field validity does not match the input frame")
                if (ec.codes_get(gid, "Nj"), ec.codes_get(gid, "Ni")) != GRID:
                    raise ValueError("GFS field is not the 0.25-degree global grid")
                if ec.codes_get(gid, "iScansNegatively") or ec.codes_get(gid, "jScansPositively") or ec.codes_get(gid, "jPointsAreConsecutive"):
                    raise ValueError("Unexpected GFS scan direction")
                if not (math.isclose(ec.codes_get(gid, "latitudeOfFirstGridPointInDegrees"), 90)
                        and math.isclose(ec.codes_get(gid, "longitudeOfFirstGridPointInDegrees"), 0)):
                    raise ValueError("Unexpected GFS grid origin")
                short = ec.codes_get(gid, "shortName")
                level_type, level = ec.codes_get(gid, "typeOfLevel"), ec.codes_get(gid, "level")
                if kind == "rain":
                    if ec.codes_get(gid, "stepType") != "accum" or ec.codes_get(gid, "endStep") - ec.codes_get(gid, "startStep") != 6:
                        raise ValueError("GFS rain is not a six-hour accumulation")
                    channel = "TP"
                elif level_type == "isobaricInhPa":
                    variable = {"gh": "Z", "t": "T", "u": "U", "v": "V", "r": "R"}.get(short)
                    if not variable or level not in LEVELS:
                        raise ValueError(f"Unexpected GFS pressure field {short}/{level}")
                    channel = f"{variable}{level}"
                else:
                    channel = {"2t": "T2M", "10u": "U10", "10v": "V10", "mslet": "MSL"}.get(short)
                    if not channel:
                        raise ValueError(f"Unexpected GFS surface field {short}")
                if ec.codes_get(gid, "numberOfMissing"):
                    raise ValueError(f"GFS {channel} has missing values; refusing zero-fill")
                values = np.asarray(ec.codes_get_values(gid), dtype=np.float32).reshape(GRID)
                if not np.isfinite(values).all():
                    raise ValueError(f"Non-finite GFS {channel}")
                units = ec.codes_get(gid, "units")
                if channel.startswith("Z"):
                    values *= 9.80665
                if channel == "TP":
                    if units == "m":
                        values *= 1000
                    elif units not in ("kg m**-2", "kg m-2", "mm"):
                        raise ValueError(f"Unknown GFS rainfall units: {units}")
                    values = np.clip(values, 0, 1000)
                yield channel, values
            finally:
                ec.codes_release(gid)


def prepare_input(output: Path, cache: Path, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    init = six_hour_time(now)
    previous = init - timedelta(hours=6)
    output.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(output.parent).free < 2 * 1024**3:
        raise RuntimeError("At least 2 GB free disk is required for current GFS input")
    downloader = GFSDownloader(cache)
    if output.is_file():
        try:
            with nc.Dataset(native_nc_path(output)) as data:
                if (data.variables["data"].shape == (2, 70, 721, 1440)
                        and data.getncattr("init_time_utc") == init.isoformat()
                        and data.getncattr("initialization_source").startswith("NOAA GFS")):
                    frames = json.loads(data.getncattr("source_frames"))
                    analysis_available = False
                    if frames[-1]["state_forecast_hours"] != 0:
                        try:
                            select_records(downloader.inventory(init, 0), "state")
                            analysis_available = True
                        except (requests.RequestException, ValueError):
                            pass
                    if not analysis_available:
                        downloader.prune_old_cache(now)
                        return {"input_path": native_nc_path(output), "init_time_utc": init.isoformat(), "cached": True}
        except (OSError, AttributeError, KeyError, ValueError, IndexError):
            pass
    sources = []
    paths = []
    for valid in (previous, init):
        cycle, hour = downloader.choose_state(valid)
        state = downloader.fetch_fields(cycle, hour, "state")
        rain_cycle = valid - timedelta(hours=6)
        rain = downloader.fetch_fields(rain_cycle, 6, "rain")
        paths.append((valid, state, rain))
        sources.append({"valid_time_utc": valid.isoformat(), "state_cycle_utc": cycle.isoformat(), "state_forecast_hours": hour,
                        "precipitation_cycle_utc": rain_cycle.isoformat(), "precipitation_forecast_hours": 6})
    pending = output.with_name(output.stem + ".pending.nc")
    with nc.Dataset(native_nc_path(pending), "w", format="NETCDF4") as data:
        for name, size in (("time", 2), ("level", 70), ("lat", 721), ("lon", 1440)):
            data.createDimension(name, size)
        time_var = data.createVariable("time", "i8", ("time",))
        time_var.units = "seconds since 1970-01-01 00:00:00"
        time_var.calendar = "proleptic_gregorian"
        time_var[:] = [int(previous.timestamp()), int(init.timestamp())]
        data.createVariable("level", str, ("level",))[:] = np.asarray(CHANNELS, dtype=object)
        data.createVariable("lat", "f8", ("lat",))[:] = np.linspace(90, -90, 721)
        data.createVariable("lon", "f8", ("lon",))[:] = np.arange(1440) * .25
        values = data.createVariable("data", "f4", ("time", "level", "lat", "lon"), zlib=True, complevel=1, chunksizes=(1, 1, 721, 1440))
        data.setncattr("init_time_utc", init.isoformat())
        data.setncattr("initialization_source", "NOAA GFS analysis/short-range background")
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
    pending.replace(output)
    downloader.prune_old_cache(now)
    return {"input_path": native_nc_path(output), "init_time_utc": init.isoformat(), "cached": False, "source_frames": sources}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=APP_ROOT / "prepared/latest.nc")
    parser.add_argument("--cache-dir", type=Path, default=APP_ROOT / "prepared/gfs_cache")
    args = parser.parse_args()
    try:
        print(json.dumps(prepare_input(args.output, args.cache_dir), ensure_ascii=False))
        return 0
    except Exception as error:
        print(json.dumps({"error": f"{type(error).__name__}: {error}"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

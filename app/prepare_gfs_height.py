"""Fetch native GFS AGL winds and surface gust/visibility, without credentials.

Prefer NOMADS regional filtering; fall back to validated S3 byte ranges. These
fields are GFS forecasts, not FuXi-corrected winds or altitude-resolved gusts.
"""

import argparse
import json
import math
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import netCDF4 as nc
import numpy as np
import requests

from prepare_gfs_live import APP_ROOT, GFSDownloader, native_nc_path
from weather_region import DEFAULT_BOUNDS


HEIGHTS = (10, 20, 30, 40, 50, 80, 100)
CHANNELS = tuple(f'{component}{height}' for height in HEIGHTS for component in ('U', 'V')) + ('GUST', 'VIS')
FILTER_URL = 'https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl'


def select_height_records(records):
    found = {}
    for record in records:
        variable, level = record['variable'], record['level']
        channel = None
        if variable in ('UGRD', 'VGRD') and level in {f'{height} m above ground' for height in HEIGHTS}:
            channel = f"{variable[0]}{level.split()[0]}"
        elif variable in ('GUST', 'VIS') and level == 'surface':
            channel = variable
        if channel:
            if channel in found:
                raise ValueError(f'Duplicate instantaneous GFS field {channel}')
            found[channel] = {**record, 'channel': channel}
    if set(found) != set(CHANNELS):
        raise ValueError(f'Missing AGL/weather fields: {sorted(set(CHANNELS) - found.keys())}')
    return sorted(found.values(), key=lambda item: item['start'])


def decode_region(path, valid, bounds=DEFAULT_BOUNDS):
    import eccodes as ec
    found, axes = {}, None
    with path.open('rb') as handle:
        while True:
            gid = ec.codes_grib_new_from_file(handle)
            if gid is None:
                break
            try:
                stamp = datetime.strptime(f"{ec.codes_get(gid, 'validityDate'):08d}{ec.codes_get(gid, 'validityTime'):04d}", '%Y%m%d%H%M').replace(tzinfo=timezone.utc)
                if stamp != valid or ec.codes_get(gid, 'stepType') != 'instant':
                    raise ValueError('Auxiliary weather time/statistic does not match request')
                if ec.codes_get(gid, 'gridType') != 'regular_ll' or ec.codes_get(gid, 'jPointsAreConsecutive'):
                    raise ValueError('Unsupported GFS auxiliary scan/grid')
                if not all(math.isclose(ec.codes_get(gid, key), .25) for key in ('iDirectionIncrementInDegrees', 'jDirectionIncrementInDegrees')):
                    raise ValueError('Auxiliary weather must preserve the native 0.25-degree grid')
                ny, nx = ec.codes_get(gid, 'Nj'), ec.codes_get(gid, 'Ni')
                lat0, lon0 = ec.codes_get(gid, 'latitudeOfFirstGridPointInDegrees'), ec.codes_get(gid, 'longitudeOfFirstGridPointInDegrees')
                lats = lat0 + np.arange(ny) * (.25 if ec.codes_get(gid, 'jScansPositively') else -.25)
                lons = lon0 + np.arange(nx) * (-.25 if ec.codes_get(gid, 'iScansNegatively') else .25)
                if ec.codes_get(gid, 'numberOfMissing'):
                    raise ValueError('Missing auxiliary weather values cannot be zero-filled')
                values = np.asarray(ec.codes_get_values(gid)).reshape(ny, nx)
                if lats[0] < lats[-1]: lats, values = lats[::-1], values[::-1]
                if lons[0] > lons[-1]: lons, values = lons[::-1], values[:, ::-1]
                west, south, east, north = bounds
                rows = np.flatnonzero((lats >= south) & (lats <= north))
                cols = np.flatnonzero((lons >= west) & (lons <= east))
                if len(rows) < 2 or len(cols) < 2:
                    raise ValueError('GFS regional filter did not cover the requested region')
                regional = values[np.ix_(rows, cols)]
                if not np.isfinite(regional).all(): raise ValueError('Non-finite auxiliary weather')
                current_axes = (lats[rows].tolist(), lons[cols].tolist())
                if current_axes[0][0] != north or current_axes[0][-1] != south or current_axes[1][0] != west or current_axes[1][-1] != east:
                    raise ValueError('Auxiliary filter returned only partial regional coverage')
                if axes is not None and axes != current_axes: raise ValueError('Auxiliary channels have different grids')
                axes = current_axes
                category, number = ec.codes_get(gid, 'parameterCategory'), ec.codes_get(gid, 'parameterNumber')
                level_type, height = ec.codes_get(gid, 'typeOfLevel'), ec.codes_get(gid, 'level')
                if level_type == 'heightAboveGround' and height in HEIGHTS and category == 2 and number in (2, 3):
                    channel = f"{'U' if number == 2 else 'V'}{height}"
                elif level_type == 'surface' and (category, number) in ((2, 22), (19, 0)):
                    channel = 'GUST' if category == 2 else 'VIS'
                else:
                    raise ValueError(f'Unexpected auxiliary GRIB field: {category}/{number}/{level_type}/{height}')
                units = ec.codes_get(gid, 'units')
                if units not in (('m',) if channel == 'VIS' else ('m s**-1', 'm s-1', 'm/s')):
                    raise ValueError(f'Unexpected {channel} units: {units}')
                if channel in ('GUST', 'VIS') and (regional < 0).any(): raise ValueError('Negative gust/visibility')
                if channel in found: raise ValueError(f'Duplicate decoded field {channel}')
                found[channel] = regional.tolist()
            finally:
                ec.codes_release(gid)
    if set(found) != set(CHANNELS): raise ValueError('Incomplete GFS auxiliary regional data')
    return {'latitudes': axes[0], 'longitudes': axes[1], 'values': [found[channel] for channel in CHANNELS]}


def cached_frame_valid(saved, cycle, hour):
    try:
        valid = cycle + timedelta(hours=hour)
        values = np.asarray(saved['values'], dtype=float)
        expected_lat = np.arange(DEFAULT_BOUNDS[3], DEFAULT_BOUNDS[1] - .1, -.25).tolist()
        expected_lon = np.arange(DEFAULT_BOUNDS[0], DEFAULT_BOUNDS[2] + .1, .25).tolist()
        return (saved.get('generator') == 'fuxi-gfs-height' and saved.get('schema_version') == 1
                and saved['valid_time_utc'] == valid.isoformat() and saved['source_cycle_utc'] == cycle.isoformat()
                and saved['source_forecast_hours'] == hour and saved['channels'] == list(CHANNELS)
                and saved['latitudes'] == expected_lat and saved['longitudes'] == expected_lon
                and values.shape == (16, len(expected_lat), len(expected_lon)) and np.isfinite(values).all()
                and (values[-2:] >= 0).all())
    except (ValueError, TypeError, KeyError): return False


def prune_aux_cache(cache, now):
    for regional in cache.glob('region_*.json'):
        match = re.fullmatch(r'region_(\d{10})_f(\d{3})\.json', regional.name)
        if not match: continue
        try:
            saved = json.loads(regional.read_text(encoding='utf-8'))
            if saved.get('generator') != 'fuxi-gfs-height': continue
            valid = datetime.fromisoformat(saved['valid_time_utc']).astimezone(timezone.utc)
            if valid >= now - timedelta(hours=48): continue
        except (ValueError, KeyError, OSError): continue
        binary = cache / f'nomads_{match[1]}_f{match[2]}.grib2'
        if regional.resolve().parent == cache.resolve() and binary.resolve().parent == cache.resolve():
            binary.unlink(missing_ok=True)
            regional.unlink()


def fetch_frame(cycle, hour, cache):
    valid = cycle + timedelta(hours=hour)
    regional = cache / f'region_{cycle:%Y%m%d%H}_f{hour:03d}.json'
    if regional.is_file():
        try:
            saved = json.loads(regional.read_text(encoding='utf-8'))
            if cached_frame_valid(saved, cycle, hour):
                return saved
        except (ValueError, KeyError): pass
    cache.mkdir(parents=True, exist_ok=True)
    binary = cache / f'nomads_{cycle:%Y%m%d%H}_f{hour:03d}.grib2'
    params = {'file': f'gfs.t{cycle:%H}z.pgrb2.0p25.f{hour:03d}',
              'dir': f'/gfs.{cycle:%Y%m%d}/{cycle:%H}/atmos', 'subregion': '',
              'leftlon': DEFAULT_BOUNDS[0], 'rightlon': DEFAULT_BOUNDS[2],
              'bottomlat': DEFAULT_BOUNDS[1], 'toplat': DEFAULT_BOUNDS[3], 'lev_surface': 'on'}
    params.update({f'lev_{height}_m_above_ground': 'on' for height in HEIGHTS})
    params.update({f'var_{variable}': 'on' for variable in ('UGRD', 'VGRD', 'VIS', 'GUST')})
    transport = 'NOMADS regional filter'
    try:
        with requests.get(FILTER_URL, params=params, timeout=(10, 60), stream=True) as response:
            response.raise_for_status()
            size = 0
            with binary.open('wb') as handle:
                for block in response.iter_content(64 * 1024):
                    size += len(block)
                    if size > 32 * 1024 * 1024: raise ValueError('Regional response exceeds expected bound')
                    handle.write(block)
        decoded = decode_region(binary, valid)
    except (requests.RequestException, ValueError, OSError) as error:
        print(f'NOMADS unavailable ({error}); trying selected S3 fields', file=sys.stderr)
        downloader = GFSDownloader(cache)
        binary = downloader.fetch_records(cycle, hour, 'height', select_height_records(downloader.inventory(cycle, hour)))
        decoded = decode_region(binary, valid)
        transport = 'NOAA S3 selected byte ranges'
    result = {**decoded, 'valid_time_utc': valid.isoformat(), 'channels': list(CHANNELS),
              'source_cycle_utc': cycle.isoformat(), 'source_forecast_hours': hour, 'transport': transport,
              'generator': 'fuxi-gfs-height', 'schema_version': 1}
    pending = regional.with_suffix('.pending.json')
    pending.write_text(json.dumps(result, allow_nan=False), encoding='utf-8')
    pending.replace(regional)
    print(f'Retained GFS AGL wind/gust/visibility {valid.isoformat()} via {transport}', file=sys.stderr)
    return result


def prepare_height(input_path, cache):
    with nc.Dataset(native_nc_path(input_path)) as data:
        init = datetime.fromisoformat(data.getncattr('init_time_utc')).astimezone(timezone.utc)
        try:
            frames = json.loads(data.getncattr('source_frames'))
            cycle = datetime.fromisoformat(frames[-1]['state_cycle_utc']).astimezone(timezone.utc)
        except (AttributeError, ValueError, KeyError):
            cycle, _ = GFSDownloader(cache).choose_state(init)
    offset = int((init - cycle).total_seconds() / 3600)
    if offset not in (0, 6, 12): raise ValueError('Auxiliary source cycle is not aligned with current initialization')
    # Two concurrent regional queries, never a bulk global-file download.
    with ThreadPoolExecutor(max_workers=2) as executor:
        frames = list(executor.map(lambda lead: fetch_frame(cycle, offset + lead, cache), (0, 6, 12, 18, 24)))
    if any((frame['latitudes'], frame['longitudes']) != (frames[0]['latitudes'], frames[0]['longitudes']) for frame in frames):
        raise ValueError('Auxiliary forecast frames use different grids')
    GFSDownloader(cache).prune_old_cache(datetime.now(timezone.utc))
    prune_aux_cache(cache, datetime.now(timezone.utc))
    return {'init_time_utc': init.isoformat(), 'source_cycle_utc': cycle.isoformat(), 'heights_m': list(HEIGHTS),
            'channels': list(CHANNELS), 'latitudes': frames[0]['latitudes'], 'longitudes': frames[0]['longitudes'],
            'native_resolution_deg': .25, 'height_reference': 'AGL',
            'snapshots': [{'valid_time_utc': frame['valid_time_utc'], 'lead_hours': lead, 'values': frame['values'],
                           'source': 'noaa_gfs', 'source_forecast_hours': frame['source_forecast_hours'], 'transport': frame['transport']}
                          for lead, frame in zip((0, 6, 12, 18, 24), frames)]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--cache-dir', type=Path, default=APP_ROOT / 'prepared/gfs_aux_cache')
    args = parser.parse_args()
    try:
        print(json.dumps(prepare_height(args.input, args.cache_dir), allow_nan=False))
        return 0
    except Exception as error:
        print(json.dumps({'error': f'{type(error).__name__}: {error}'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

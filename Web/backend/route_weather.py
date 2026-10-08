"""Align nominal along-route arrival times with native regional weather."""

import math
from datetime import timedelta

from weather_service import WeatherUnavailable, iso_utc, utc_timestamp


def segment_distance(a, b):
    lon1, lat1, lon2, lat2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371008.8 * math.asin(min(1., math.sqrt(h)))


def route_positions(coordinates):
    if not 2 <= len(coordinates) <= 10000:
        raise WeatherUnavailable("路线须包含 2–10000 个坐标点。")
    cleaned = []
    for point in coordinates:
        if len(point) != 2 or not all(math.isfinite(v) for v in point) or not (-180 <= point[0] <= 180 and -90 <= point[1] <= 90):
            raise WeatherUnavailable("航线坐标必须是有效的 [经度, 纬度]。")
        if not cleaned or point != cleaned[-1]:
            cleaned.append(point)
    lengths = [segment_distance(a, b) for a, b in zip(cleaned, cleaned[1:])]
    total = sum(lengths)
    if total <= 0:
        raise WeatherUnavailable("路线长度必须大于零。")
    # Retain all vertices (so corners are not skipped), additionally sample long
    # segments at <=1 km. Refuse excessively dense input, rather than silently
    # dropping route bends or assigning weather to a straight endpoint chord.
    if sum(max(1, math.ceil(length / 1000)) for length in lengths) > 2000:
        raise WeatherUnavailable("路线采样超过 2000 段，请缩小任务范围。")
    positions, traversed = [], 0.
    for index, (a, b, length) in enumerate(zip(cleaned, cleaned[1:], lengths)):
        count = max(1, math.ceil(length / 1000))
        for step in range(count):
            fraction = step / count
            positions.append({'coordinate': [a[0] + (b[0] - a[0]) * fraction, a[1] + (b[1] - a[1]) * fraction],
                              'distance_m': traversed + length * fraction, 'source_segment': index})
        traversed += length
    positions.append({'coordinate': list(cleaned[-1]), 'distance_m': total, 'source_segment': len(lengths) - 1})
    return positions, total


def align_route(grid, coordinates, departure_time_utc, speed_kmh, altitude_m, model=None, now=None,
                height_grid=None, height_warning=None):
    if not math.isfinite(speed_kmh) or not 0 < speed_kmh <= 200:
        raise WeatherUnavailable("任务速度须大于零且不超过 200 km/h。")
    if not math.isfinite(altitude_m) or not 0 <= altitude_m <= 1000:
        raise WeatherUnavailable("任务高度须在 0–1000 米范围内。")
    departure = utc_timestamp(departure_time_utc)
    if now is not None and departure < now:
        raise WeatherUnavailable("出发时间已过去，请选择当前或未来时刻。")
    positions, total = route_positions(coordinates)
    samples, missing, height_missing = [], [], []
    for index, position in enumerate(positions):
        when = departure + timedelta(seconds=position['distance_m'] / (speed_kmh / 3.6))
        base = {'index': index, **position, 'eta_utc': when.isoformat()}
        try:
            longitude, latitude = position['coordinate']
            forecast = grid.sample(latitude, longitude, model, when.isoformat())
            height_weather = None
            try:
                if height_grid is None:
                    raise WeatherUnavailable(height_warning or '近地高度风、阵风和能见度资料尚不可用。')
                height_weather = height_grid.sample_height(latitude, longitude, altitude_m, when.isoformat())
            except WeatherUnavailable as error:
                height_missing.append({**base, 'reason': str(error)})
            samples.append({**base, 'weather': forecast, 'height_weather': height_weather})
        except WeatherUnavailable as error:
            missing.append({**base, 'reason': str(error)})
    return {
        'state': 'incomplete' if missing else 'time_aligned_surface_only' if height_missing else 'space_time_height_aligned',
        'departure_time_utc': iso_utc(departure),
        'arrival_time_utc': (departure + timedelta(seconds=total / (speed_kmh / 3.6))).isoformat(),
        'distance_m': total, 'duration_seconds': total / (speed_kmh / 3.6),
        'altitude_m': altitude_m, 'height_reference': 'AGL', 'sample_count': len(positions),
        'samples': samples, 'missing': missing, 'height_missing': height_missing,
        'coverage': {'surface_space_time': not missing, 'flight_height': not missing and not height_missing},
        'native_time_step_hours': 6, 'native_resolution_deg': .25,
        'eta_method': 'constant_user_speed_no_wind_adjustment',
        'message': ('沿线天气未完全匹配，覆盖不足的位置与原因已列出，不进行空间或时间外推。' if missing
                    else '沿线地面时空已匹配，但高度天气未完全匹配。' if height_missing
                    else '沿线位置、经过时刻与离地高度风已匹配；插值是估计，阵风和能见度仍为地面指标。'),
        'eta_warning': 'ETA 按填写速度估算，未计入风对地速的影响。',
        'flight_permission': None,
    }

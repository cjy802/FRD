"""Native GFS AGL wind sampling. Surface gust/VIS remain surface indicators."""

import math

from regional_weather import RegionalWeather
from weather_service import WeatherUnavailable, utc_timestamp


HEIGHTS = (10, 20, 30, 40, 50, 80, 100)
CHANNELS = tuple(f'{component}{height}' for height in HEIGHTS for component in ('U', 'V')) + ('GUST', 'VIS')


class HeightWeather(RegionalWeather):
    def __init__(self, payload, init_time):
        if payload.get('height_reference') != 'AGL' or tuple(payload.get('heights_m', ())) != HEIGHTS:
            raise WeatherUnavailable('近地风资料的离地高度定义或层次不匹配。')
        if utc_timestamp(payload['init_time_utc']) != utc_timestamp(init_time):
            raise WeatherUnavailable('近地风资料与 FuXi 起报时刻不匹配。')
        self.source_cycle = utc_timestamp(payload['source_cycle_utc'])
        if (utc_timestamp(init_time) - self.source_cycle).total_seconds() not in (0, 6 * 3600, 12 * 3600):
            raise WeatherUnavailable('近地风资料来源周期过旧或晚于起报时刻。')
        super().__init__(payload, init_time, 'NOAA GFS auxiliary forecasts', required_channels=CHANNELS)
        if any((item['values'][self.channels.index(channel)] < 0).any() for item in self.snapshots for channel in ('GUST', 'VIS')):
            raise WeatherUnavailable('阵风或能见度资料含负值。')

    def sample_height(self, latitude, longitude, altitude_m, valid_time_utc):
        if not math.isfinite(altitude_m) or not HEIGHTS[0] <= altitude_m <= HEIGHTS[-1]:
            raise WeatherUnavailable('目前高度风覆盖离地 10–100 米；不进行高度外推。')
        when = utc_timestamp(valid_time_utc)
        left, right, fraction = self.time_bracket(when)
        vector = self.vector(latitude, longitude, left)
        if fraction: vector = vector * (1 - fraction) + self.vector(latitude, longitude, right) * fraction
        upper = next(height for height in HEIGHTS if height >= altitude_m)
        lower = upper if altitude_m == upper else HEIGHTS[HEIGHTS.index(upper) - 1]
        weight = 0. if lower == upper else (altitude_m - lower) / (upper - lower)
        def wind(component):
            return float(vector[self.channels.index(f'{component}{lower}')] * (1 - weight)
                         + vector[self.channels.index(f'{component}{upper}')] * weight)
        u, v = wind('U'), wind('V')
        return {
            'height_m': altitude_m, 'height_reference': 'AGL',
            'height_method': 'native_height' if lower == upper else 'linear_height_estimate',
            'height_bracket_m': [lower, upper],
            'time_method': 'native_time' if left is right else 'linear_time_estimate',
            'time_bracket_utc': [left['valid'].isoformat(), right['valid'].isoformat()],
            'valid_time_utc': when.isoformat(), 'source_cycle_utc': self.source_cycle.isoformat(),
            'source_forecast_hours': (when - self.source_cycle).total_seconds() / 3600,
            'source': 'noaa_gfs', 'native_resolution_deg': .25,
            'u_m_s': u, 'v_m_s': v, 'wind_speed_m_s': math.hypot(u, v),
            'gust_surface_m_s': float(vector[self.channels.index('GUST')]),
            'visibility_surface_m': float(vector[self.channels.index('VIS')]),
            'hazard_scope': 'gust_and_visibility_are_surface_indicators',
            'warning': '阵风和能见度为地面指标，不代表任务高度的阵风、云中能见度或湍流。',
        }

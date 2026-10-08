"""Native regional weather sampling with explicit calibration coverage."""

import math
from datetime import timedelta

import numpy as np

from weather_service import FIELDS, WeatherUnavailable, assemble_forecast, utc_timestamp


FIELD_CHANNELS = dict(zip(FIELDS, ("T2M", "U10", "V10", "MSL", "TP")))
CALIBRATION_POINT = (31.5, 119.5)


class RegionalWeather:
    def __init__(self, payload: dict, init_time: str, input_source: str, required_channels=None):
        self.latitudes = np.asarray(payload["latitudes"], dtype=float)
        self.longitudes = np.asarray(payload["longitudes"], dtype=float)
        self.channels = list(payload["channels"])
        self.init = utc_timestamp(init_time)
        self.input_source = input_source
        if (self.latitudes.ndim != 1 or self.longitudes.ndim != 1
                or len(self.latitudes) < 2 or len(self.longitudes) < 2
                or not np.isfinite(self.latitudes).all() or not np.isfinite(self.longitudes).all()
                or len(set(self.channels)) != len(self.channels)):
            raise WeatherUnavailable("区域天气坐标或通道不完整。")
        if not (np.allclose(np.diff(self.latitudes), -.25) and np.allclose(np.diff(self.longitudes), .25)):
            raise WeatherUnavailable("区域天气必须保留原生 0.25° 网格。")
        required = set(FIELD_CHANNELS.values()) if required_channels is None else set(required_channels)
        if not required.issubset(self.channels):
            raise WeatherUnavailable("区域天气缺少地面预报变量。")
        self.snapshots = []
        for snapshot in payload["snapshots"]:
            values = np.asarray(snapshot["values"], dtype=np.float64)
            if values.shape != (len(self.channels), len(self.latitudes), len(self.longitudes)) or not np.isfinite(values).all():
                raise WeatherUnavailable("区域天气矩阵无效。")
            valid = utc_timestamp(snapshot["valid_time_utc"])
            lead = snapshot["lead_hours"]
            if not isinstance(lead, int) or lead < 0 or lead % 6 or (valid - self.init).total_seconds() != lead * 3600:
                raise WeatherUnavailable("区域天气时刻与提前量不一致。")
            self.snapshots.append({"valid": valid, "lead_hours": lead, "values": values,
                                   "source": snapshot.get("source", "initial_background" if lead == 0 else "fuxi")})
        self.snapshots.sort(key=lambda snapshot: snapshot["valid"])
        if not self.snapshots or len({item['valid'] for item in self.snapshots}) != len(self.snapshots):
            raise WeatherUnavailable("区域天气没有有效时次。")

    def coverage(self) -> dict:
        return {
            "west": float(self.longitudes[0]), "east": float(self.longitudes[-1]),
            "south": float(self.latitudes[-1]), "north": float(self.latitudes[0]),
            "native_resolution_deg": .25, "channels": self.channels,
            "valid_times_utc": [snapshot["valid"].isoformat() for snapshot in self.snapshots],
        }

    def vector(self, latitude: float, longitude: float, snapshot: dict) -> np.ndarray:
        if not (math.isfinite(latitude) and math.isfinite(longitude)
                and self.latitudes[-1] <= latitude <= self.latitudes[0]
                and self.longitudes[0] <= longitude <= self.longitudes[-1]):
            raise WeatherUnavailable("航线位置超出本次区域天气覆盖范围。")
        row = (self.latitudes[0] - latitude) / .25
        col = (longitude - self.longitudes[0]) / .25
        i = min(int(math.floor(row)), len(self.latitudes) - 2)
        j = min(int(math.floor(col)), len(self.longitudes) - 2)
        a, b = row - i, col - j
        values = snapshot["values"]
        return ((1-a)*(1-b)*values[:, i, j] + (1-a)*b*values[:, i, j+1]
                + a*(1-b)*values[:, i+1, j] + a*b*values[:, i+1, j+1])

    def time_bracket(self, when):
        if not self.snapshots[0]['valid'] <= when <= self.snapshots[-1]['valid']:
            raise WeatherUnavailable("任务时刻超出本次天气资料覆盖范围，不进行时间外推。")
        for index, snapshot in enumerate(self.snapshots):
            if snapshot['valid'] == when:
                return snapshot, snapshot, 0.
            if snapshot['valid'] > when:
                previous = self.snapshots[index - 1]
                if snapshot['valid'] - previous['valid'] != timedelta(hours=6):
                    raise WeatherUnavailable("相邻天气时次不连续，不能插值。")
                fraction = (when - previous['valid']).total_seconds() / (6 * 3600)
                return previous, snapshot, fraction
        raise WeatherUnavailable("天气时刻未匹配。")

    def sample(self, latitude: float, longitude: float, model: dict | None = None,
               valid_time_utc: str | None = None) -> dict:
        default = next((item for item in self.snapshots if item["lead_hours"] == 6), self.snapshots[-1])
        when = utc_timestamp(valid_time_utc) if valid_time_utc else default['valid']
        left, right, fraction = self.time_bracket(when)
        vector = self.vector(latitude, longitude, left)
        if fraction:
            vector = vector * (1 - fraction) + self.vector(latitude, longitude, right) * fraction
        rain = next((item for item in self.snapshots if item['lead_hours'] > 0
                     and item['valid'] - timedelta(hours=6) <= when <= item['valid']), None)
        if rain is None:
            raise WeatherUnavailable("任务时刻没有对应的六小时累计降水预报。")
        raw = {field: float(vector[self.channels.index(channel)]) for field, channel in FIELD_CHANNELS.items()}
        raw['tp_6h_mm'] = max(0., float(self.vector(latitude, longitude, rain)[self.channels.index('TP')]))
        at_point = math.isclose(latitude, CALIBRATION_POINT[0], abs_tol=1e-6, rel_tol=0) and math.isclose(longitude, CALIBRATION_POINT[1], abs_tol=1e-6, rel_tol=0)
        # Deployment training covers native leads 6/12/24, never lead 0/18 or
        # intermediate times. Do not transfer a surface corrector into these.
        applicable = at_point and left is right and left['lead_hours'] in (6, 12, 24)
        row = {"init": self.init, "valid": when, "lead_hours": (when - self.init).total_seconds() / 3600,
               "latitude_deg": latitude, "longitude_deg": longitude, "raw": raw}
        result = assemble_forecast(row, model if applicable else None, historical=False)
        result.update({"input_source": self.input_source, "spatial_method": "bilinear_native_grid",
                       "native_resolution_deg": .25, "corrector_applicable": applicable,
                       "time_method": "native_time" if left is right else "linear_time_estimate",
                       "time_bracket_utc": [left['valid'].isoformat(), right['valid'].isoformat()],
                       "rain_interval": {"start_utc": (rain['valid'] - timedelta(hours=6)).isoformat(),
                                         "end_utc": rain['valid'].isoformat(), "unit": "mm/6h",
                                         "represents_instantaneous_intensity": False}})
        for field in FIELDS:
            if result['sources'][field] != 'ridge':
                result['sources'][field] = rain['source'] if field == 'tp_6h_mm' else (
                    left['source'] if left is right else 'temporal_interpolation_initial_fuxi' if left['lead_hours'] == 0 else 'temporal_interpolation_fuxi')
        if not applicable:
            result["model_warning"] = ("单点订正模型未在该位置验证，当前使用区域原始预报。" if not at_point
                                       else "订正模型不用于起报场、18h 或时间插值结果，当前使用未订正天气资料。")
        return result

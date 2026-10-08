"""Explainable weather screening, never flight authorization or probability."""

import math

from weather_service import WeatherUnavailable


DEMO_LIMITS = {
    'max_wind_m_s': 12., 'max_crosswind_m_s': 8., 'max_headwind_m_s': 12.,
    'max_gust_m_s': 15., 'max_rain_6h_mm': 5., 'min_visibility_m': 3000.,
}
LABELS = {'wind': ('任务高度风速', 'm/s', 'max_wind_m_s'),
          'crosswind': ('侧风', 'm/s', 'max_crosswind_m_s'),
          'headwind': ('逆风', 'm/s', 'max_headwind_m_s'),
          'gust': ('地面阵风', 'm/s', 'max_gust_m_s'),
          'rain': ('六小时累计降水', 'mm/6h', 'max_rain_6h_mm'),
          'visibility': ('地面能见度', 'm', 'min_visibility_m')}
LEVEL_LABELS = {'incomplete': '气象覆盖不足', 'no_trigger': '未触发沿线阈值',
                'attention': '沿线气象关注', 'elevated': '沿线较强提示'}


def resolve_limits(profile=None):
    if profile is None: return {**DEMO_LIMITS, 'name': '演示规则'}, 'demo'
    limits = {'name': str(profile.get('name') or '自定义阈值')[:80]}
    for field in DEMO_LIMITS:
        value = profile.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise WeatherUnavailable(f'自定义天气限值 {field} 必须是正的有限数值。')
        if value > (100000 if field == 'min_visibility_m' else 500 if field == 'max_rain_6h_mm' else 200):
            raise WeatherUnavailable(f'自定义天气限值 {field} 超出输入范围。')
        limits[field] = float(value)
    return limits, 'custom_unverified'


def bearing_degrees(a, b):
    lon1, lat1, lon2, lat2 = map(math.radians, (*a, *b))
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return math.degrees(math.atan2(x, y)) % 360


def wind_components(u, v, bearing):
    angle = math.radians(bearing)
    along = u * math.sin(angle) + v * math.cos(angle)
    signed_cross = u * math.cos(angle) - v * math.sin(angle)
    return {'along_wind_m_s': along, 'headwind_m_s': max(0., -along),
            'tailwind_m_s': max(0., along), 'crosswind_m_s': abs(signed_cross),
            'signed_crosswind_m_s': signed_cross}


def screen_values(values, limits):
    triggers, ratios = [], []
    for field, (label, unit, limit_field) in LABELS.items():
        value = values.get(field)
        if value is None: continue
        if not math.isfinite(value) or value < 0: raise WeatherUnavailable('初筛变量无效。')
        limit = limits[limit_field]
        ratio = limit / max(1., value) if field == 'visibility' else value / limit
        ratios.append(ratio)
        if ratio >= .8 - 1e-12:
            elevated = ratio >= 1. - 1e-12
            threshold = limit if elevated else limit / .8 if field == 'visibility' else limit * .8
            operator = '≤' if field == 'visibility' else '≥'
            triggers.append({'field': field, 'label': label, 'value': value, 'unit': unit,
                             'threshold': threshold, 'limit': limit, 'operator': operator,
                             'level': 'elevated' if elevated else 'attention', 'ratio': ratio,
                             'reason': f'{label} {value:.2f} {unit} {operator} {threshold:.2f} {unit}'})
    level = 'elevated' if any(item['level'] == 'elevated' for item in triggers) else 'attention' if triggers else 'no_trigger'
    return {'level': level, 'score_0_100': round(min(100., max(ratios, default=0.) * 50), 1), 'triggers': triggers}


def screen_route(alignment, coordinates, profile=None):
    limits, mode = resolve_limits(profile)
    cleaned = []
    for coordinate in coordinates:
        if not cleaned or coordinate != cleaned[-1]: cleaned.append(coordinate)
    bearings = [bearing_degrees(a, b) for a, b in zip(cleaned, cleaned[1:])]
    points, values_by_point, alerts = [], [], {}
    for sample in alignment['samples']:
        bearing = bearings[sample['source_segment']]
        height = sample.get('height_weather')
        components = wind_components(height['u_m_s'], height['v_m_s'], bearing) if height else None
        values = {'rain': sample['weather']['values']['tp_6h_mm'],
                  'wind': height['wind_speed_m_s'] if height else None,
                  'crosswind': components['crosswind_m_s'] if components else None,
                  'headwind': components['headwind_m_s'] if components else None,
                  'gust': height['gust_surface_m_s'] if height else None,
                  'visibility': height['visibility_surface_m'] if height else None}
        screening = screen_values(values, limits)
        point = {**screening, 'index': sample['index'], 'coordinate': sample['coordinate'],
                 'eta_utc': sample['eta_utc'], 'bearing_deg': bearing, 'wind_components': components,
                 'covered': height is not None}
        points.append(point)
        values_by_point.append({**values, 'tailwind': components['tailwind_m_s'] if components else None})
        for trigger in screening['triggers']:
            field = trigger['field']
            if field not in alerts or trigger['ratio'] > alerts[field]['ratio']:
                alerts[field] = {**trigger, 'point_index': point['index'], 'coordinate': point['coordinate'], 'eta_utc': point['eta_utc']}
    covered = alignment['coverage']['flight_height']
    hazard_level = 'elevated' if any(point['level'] == 'elevated' for point in points) else 'attention' if any(point['level'] == 'attention' for point in points) else 'no_trigger'
    level = hazard_level if covered else 'incomplete'
    def maximum(field):
        values = [point[field] for point in values_by_point if point[field] is not None]
        return max(values) if values else None
    visibilities = [point['visibility'] for point in values_by_point if point['visibility'] is not None]
    lookup = {point['index']: point for point in points}
    location = {item['index']: item['coordinate'] for item in [*alignment['samples'], *alignment['missing']]}
    segments = []
    for index in range(alignment['sample_count'] - 1):
        point, following = lookup.get(index), lookup.get(index + 1)
        complete = bool(point and following and point['covered'] and following['covered'])
        segments.append({'index': index, 'start': location[index], 'end': location[index + 1],
                         'covered': complete, 'level': point['level'] if complete else 'incomplete',
                         'score_0_100': point['score_0_100'] if complete else None})
    return {
        'level': level, 'label': LEVEL_LABELS[level], 'hazard_level': hazard_level,
        'score_0_100': max((point['score_0_100'] for point in points), default=0.) if covered else None,
        'score_definition': '各项数值/限值的最大比值 × 50，封顶 100；能见度使用限值/数值。关注从 80% 开始。不是事故概率。',
        'rule_mode': mode, 'limits': limits, 'coverage_complete': covered,
        'points': points, 'segments': segments, 'alerts': sorted(alerts.values(), key=lambda item: -item['ratio']),
        'triggered_point_count': sum(bool(point['triggers']) for point in points),
        'summary': {'max_wind_m_s': maximum('wind'), 'max_crosswind_m_s': maximum('crosswind'),
                    'max_headwind_m_s': maximum('headwind'), 'max_tailwind_m_s': maximum('tailwind'),
                    'max_surface_gust_m_s': maximum('gust'), 'min_surface_visibility_m': min(visibilities) if visibilities else None,
                    'max_rain_6h_mm': maximum('rain')},
        'limitations': ['阈值为演示规则或用户填写的未核验限值，不是机型认证。',
                        '天气为 0.25° 网格与 6 小时时次，插值不增加原生分辨率。',
                        '阵风/能见度是地面指标；未覆盖雷暴、结冰、云底和空中湍流。',
                        'ETA 未计入风对地速、电池续航和起降阶段的影响。'],
        'flight_permission': None,
    }

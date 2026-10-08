"""Compare covered departure slots, not continuous operational flight windows."""

import math
from datetime import timedelta

from route_weather import route_positions
from weather_service import WeatherUnavailable, utc_timestamp


def compare_departures(evaluate, native_times, coordinates, selected_departure, speed_kmh, coverage_end, now):
    if not math.isfinite(speed_kmh) or not 0 < speed_kmh <= 200: raise WeatherUnavailable('任务速度无效。')
    _, distance = route_positions(coordinates)
    duration = distance / (speed_kmh / 3.6)
    selected = utc_timestamp(selected_departure)
    times = sorted({selected, *(stamp for stamp in native_times if stamp >= now)})
    candidates = []
    for stamp in times:
        base = {'departure_time_utc': stamp.isoformat(), 'arrival_time_utc': (stamp + timedelta(seconds=duration)).isoformat(),
                'native_departure': stamp in native_times, 'selected': stamp == selected,
                'covered': False, 'level': 'incomplete', 'score_0_100': None, 'summary': None}
        if stamp < now:
            candidates.append({**base, 'reason': '出发时间已过去。'})
            continue
        if stamp + timedelta(seconds=duration) > coverage_end:
            candidates.append({**base, 'reason': '路线完成时刻超出天气资料范围，不能评估完整任务。'})
            continue
        try:
            result = evaluate(stamp.isoformat())
            risk = result['weather_risk']
            missing = [*result['missing'], *result['height_missing']]
            candidates.append({**base, 'covered': risk['coverage_complete'], 'level': risk['level'],
                               'label': risk['label'], 'score_0_100': risk['score_0_100'], 'summary': risk['summary'],
                               'reason': missing[0]['reason'] if missing else '仅比较已覆盖气象变量，不代表可以放飞。'})
        except WeatherUnavailable as error:
            candidates.append({**base, 'reason': str(error)})
    priority = {'no_trigger': 0, 'attention': 1, 'elevated': 2, 'incomplete': 3}
    candidates.sort(key=lambda item: (not item['covered'], priority[item['level']],
                                       item['score_0_100'] if item['score_0_100'] is not None else math.inf,
                                       item['departure_time_utc']))
    covered = [item for item in candidates if item['covered']]
    return {'state': 'available' if covered else 'no_covered_candidate', 'candidates': candidates,
            'best_candidate': covered[0] if covered else None,
            'covered_count': len(covered), 'native_time_step_hours': 6, 'native_resolution_deg': .25,
            'coverage_end_utc': coverage_end.isoformat(), 'flight_permission': None,
            'ranking_basis': '先完整覆盖，再按初筛等级、参考分、较早出发时刻排序；不比较事故概率或认证飞行性能。',
            'message': '只比较当前选择和未来原生预报时次；不是连续的可飞行时间段，也不代表放飞许可。'}

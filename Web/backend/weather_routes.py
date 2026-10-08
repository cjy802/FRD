"""Conservative comparison of sampled road candidates on a coarse weather grid."""

import math
from weather_service import WeatherUnavailable


def native_footprint(grid, coordinates):
    cells = set()
    for longitude, latitude in coordinates:
        if not (grid.latitudes[-1] <= latitude <= grid.latitudes[0] and grid.longitudes[0] <= longitude <= grid.longitudes[-1]):
            return None
        row = min(math.floor((grid.latitudes[0] - latitude) / .25), len(grid.latitudes) - 2)
        col = min(math.floor((longitude - grid.longitudes[0]) / .25), len(grid.longitudes) - 2)
        cells.add((row, col))
    return cells


def compare_road_weather(routes, grid, evaluate):
    if not routes: raise WeatherUnavailable('没有可比较的道路候选。')
    comparisons = []
    for index, route in enumerate(routes):
        assessment = evaluate(route['geometry']['coordinates'])
        risk = assessment['weather_risk']
        coordinates = [item['coordinate'] for item in [*assessment['samples'], *assessment['missing']]]
        cells = native_footprint(grid, coordinates)
        comparisons.append({'route_id': f'road_{index}', 'label': '道路基线' if index == 0 else f'道路备选 {index}',
                            'route': route, 'weather': {key: risk[key] for key in ('level', 'label', 'score_0_100', 'coverage_complete', 'summary', 'rule_mode')},
                            'native_cells': sorted(cells) if cells is not None else None})
    baseline = comparisons[0]
    eligible = []
    priority = {'no_trigger': 0, 'attention': 1, 'elevated': 2, 'incomplete': 3}
    for candidate in comparisons[1:]:
        base, other = baseline['weather'], candidate['weather']
        new_cells = candidate['native_cells'] != baseline['native_cells']
        delta = {}
        for field in ('max_wind_m_s', 'max_surface_gust_m_s', 'max_rain_6h_mm', 'min_surface_visibility_m'):
            a, b = base['summary'][field], other['summary'][field]
            delta[field] = abs(a - b) if a is not None and b is not None else None
        significant = any(delta[field] is not None and delta[field] >= threshold for field, threshold in
                          (('max_wind_m_s', 2.), ('max_surface_gust_m_s', 2.), ('max_rain_6h_mm', 1.), ('min_surface_visibility_m', 1000.)))
        complete = base['coverage_complete'] and other['coverage_complete']
        improves = complete and priority[other['level']] <= priority[base['level']] and other['score_0_100'] <= base['score_0_100'] - 5
        reasonable_length = candidate['route']['distance_m'] <= baseline['route']['distance_m'] * 1.8
        candidate['comparison'] = {'different_native_cells': new_cells, 'weather_delta': delta,
                                   'significant_weather_difference': significant, 'lower_screening_score': bool(improves),
                                   'within_detour_budget': reasonable_length}
        if complete and new_cells and significant and improves and reasonable_length: eligible.append(candidate)
    if eligible:
        best = min(eligible, key=lambda item: (priority[item['weather']['level']], item['weather']['score_0_100'], item['route']['distance_m']))
        state, reason = 'candidate_lower_screening', '所比较备选跨入不同原生网格，已覆盖气象变量差异达到演示门槛且初筛分较低；仍需核验，不能据此认定可飞。'
    else:
        best = baseline
        if len(comparisons) == 1:
            state, reason = 'no_alternative', '在当前道路和建筑约束下没有生成适度长度的不同备选；保留道路基线。'
        elif not baseline['weather']['coverage_complete'] or not any(item['weather']['coverage_complete'] for item in comparisons[1:]):
            state, reason = 'coverage_incomplete', '候选缺少完整天气覆盖，不能比较可靠天气收益；保留道路基线，但不代表基线可飞。'
        elif all(item['native_cells'] == baseline['native_cells'] for item in comparisons[1:]):
            state, reason = 'no_resolvable_weather_difference', '候选使用相同原生天气网格；校园/街道尺度的插值或航向差异不足以证明天气绕行收益，保留道路基线。'
        else:
            state, reason = 'no_material_improvement', '没有同时满足不同网格、天气差异、初筛改善和绕行长度约束的备选；保留道路基线。'
    return {'state': state, 'reason': reason, 'candidates': comparisons, 'recommended_route_id': best['route_id'],
            'native_resolution_deg': .25, 'flight_permission': None,
            'comparison_rules': {'minimum_wind_or_gust_difference_m_s': 2, 'minimum_rain_difference_mm_6h': 1,
                                 'minimum_visibility_difference_m': 1000, 'minimum_score_improvement': 5, 'maximum_distance_ratio': 1.8},
            'message': '仅比较这些道路参考候选，不保证全局最优；天气变化门槛是演示规则，不是已验证精度或飞行许可。'}

"""Separate screened weather variables from unresolved flight conditions."""


def build_decision(assessment):
    risk = assessment['weather_risk']
    covered = risk['coverage_complete']
    labels = {
        'incomplete': '气象覆盖不足，需补齐后重新初筛',
        'elevated': '气象较强提示，需调整任务并核验',
        'attention': '气象关注，放飞条件仍待核验',
        'no_trigger': '已覆盖气象变量未触发阈值，放飞条件仍待核验',
    }
    checks = [
        {'key': 'prediction', 'label': '区域天气预测', 'state': 'ready', 'detail': '真实 FuXi 区域预报已生成。'},
        {'key': 'surface_space_time', 'label': '地面位置与时间', 'state': 'ready' if assessment['coverage']['surface_space_time'] else 'missing',
         'detail': '沿线位置与 ETA 已匹配。' if assessment['coverage']['surface_space_time'] else assessment['missing'][0]['reason']},
        {'key': 'height', 'label': '任务高度风', 'state': 'ready' if covered else 'missing',
         'detail': '10–100 米 AGL 原生高度层或层间估计。' if covered else assessment['height_missing'][0]['reason'] if assessment['height_missing'] else '存在沿线时空缺测。'},
        {'key': 'weather_screen', 'label': '已覆盖变量初筛', 'state': risk['level'], 'detail': risk['label']},
        {'key': 'aircraft', 'label': '机型与适用工况', 'state': 'not_configured' if risk['rule_mode'] == 'demo' else 'provided_unverified',
         'detail': '尚未配置机型限制；使用演示规则。' if risk['rule_mode'] == 'demo' else '自定义阈值已填写，尚未核验机型手册和适用工况。'},
        {'key': 'airspace', 'label': '空域与任务审批', 'state': 'not_verified', 'detail': '未接入权威空域限制或任务审批结果。'},
        {'key': 'obstacles', 'label': '空中障碍与起降点', 'state': 'not_verified', 'detail': '二维道路/建筑参考不包含已核验的线缆、树木、建筑高度和现场起降条件。'},
        {'key': 'other_weather', 'label': '其他气象危险', 'state': 'not_covered', 'detail': '未覆盖雷暴、结冰、云底与任务高度湍流；地面能见度不等于云中能见度。'},
    ]
    return {'status': risk['level'], 'label': labels[risk['level']], 'checks': checks,
            'flight_permission': None, 'may_fly': None,
            'message': '系统仅提供有限变量的气象初筛和道路参考，不签发放飞许可。'}

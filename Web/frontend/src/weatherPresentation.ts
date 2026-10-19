import type { RouteAssessment, WeatherRisk } from './weatherTypes'

const weatherCheckKeys = new Set(['prediction', 'surface_space_time', 'height', 'weather_screen'])

export function weatherOnlyChecks(decision: RouteAssessment['decision']) {
  return decision.checks.filter(check => weatherCheckKeys.has(check.key))
}

export function weatherSummary(risk: Pick<WeatherRisk, 'level' | 'label'>) {
  const labels: Record<string, string> = {
    incomplete: '气象覆盖不足，暂不能完成沿线初筛',
    elevated: '沿线有指标达到较强提醒线，相关指标已用红框标出',
    attention: '沿线有指标接近提醒线，相关指标已用橙框标出',
    no_trigger: '已覆盖天气指标未触发当前阈值',
  }
  return labels[risk.level] || risk.label
}

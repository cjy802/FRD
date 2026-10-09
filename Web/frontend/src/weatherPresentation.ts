import type { RouteAssessment, WeatherRisk } from './weatherTypes'

const weatherCheckKeys = new Set(['prediction', 'surface_space_time', 'height', 'weather_screen'])

export function weatherOnlyChecks(decision: RouteAssessment['decision']) {
  return decision.checks.filter(check => weatherCheckKeys.has(check.key))
}

export function weatherSummary(risk: Pick<WeatherRisk, 'level' | 'label'>) {
  const labels: Record<string, string> = {
    incomplete: '气象覆盖不足，暂不能完成沿线初筛',
    elevated: '沿线气象较强提示，请查看触发位置和数值',
    attention: '沿线气象关注，请查看当前阈值提示',
    no_trigger: '已覆盖天气指标未触发当前阈值',
  }
  return labels[risk.level] || risk.label
}

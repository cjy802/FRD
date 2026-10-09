import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

const source = await readFile(new URL('../src/weatherPresentation.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
}).outputText
const { weatherOnlyChecks, weatherSummary } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)

const keys = ['prediction', 'surface_space_time', 'height', 'weather_screen', 'aircraft', 'airspace', 'obstacles', 'other_weather', 'future_non_weather_check']
const decision = { checks: keys.map(key => ({ key, label: key, state: 'ready', detail: key })) }
const checks = weatherOnlyChecks(decision)
assert.deepEqual(checks.map(check => check.key), keys.slice(0, 4))
assert.equal(decision.checks.length, 9, 'Presentation must not mutate the backend assessment')
assert.deepEqual(weatherOnlyChecks({ checks: [] }), [])
for (const level of ['incomplete', 'elevated', 'attention', 'no_trigger']) {
  const label = weatherSummary({ level, label: 'fallback' })
  assert.match(label, /气象|天气/)
  assert.doesNotMatch(label, /放飞|机型|审批|许可|空域|障碍/)
}
assert.match(weatherSummary({ level: 'incomplete', label: '' }), /覆盖不足/)
assert.match(weatherSummary({ level: 'no_trigger', label: '' }), /已覆盖天气指标/)
assert.equal(weatherSummary({ level: 'unknown', label: '天气结果暂不可用' }), '天气结果暂不可用')
console.log('Weather-only presentation checks passed: filtering, all four statuses, missing data, and immutable API checks.')

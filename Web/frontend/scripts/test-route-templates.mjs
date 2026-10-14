import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import ts from 'typescript'

const source = await readFile(new URL('../src/routeTemplates.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText

const calls = []
const responses = []
const reply = (status, body) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => {
    if (status === 204) throw new SyntaxError('No response body')
    return body
  },
})
const context = vm.createContext({
  exports: {},
  fetch: async (url, init) => {
    calls.push({ url, init })
    assert.ok(responses.length, 'Each request needs a queued response')
    return responses.shift()
  },
  encodeURIComponent,
  JSON,
})
new vm.Script(compiled, { filename: 'routeTemplates.ts' }).runInContext(context)
const api = context.exports

const payload = {
  schema_version: 1,
  plan_input: {
    anchors: [
      { id: 'anchor-1', latitude: 31.68, longitude: 119.95 },
      { id: 'anchor-2', latitude: 31.69, longitude: 119.96 },
    ],
    road_source: 'local', altitude_m: 100, speed_kmh: 45, clearance_m: 2,
  },
  route_snapshot: { geometry: { coordinates: [[119.95, 31.68], [119.96, 31.69]] } },
  waypoint_settings: [
    { altitude_m: 100, heading_mode: 'fixed', heading_angle: 90, gimbal_pitch_angle: -30, export_anchor: true },
    { altitude_m: 100, heading_mode: 'followWayline', heading_angle: 90, gimbal_pitch_angle: 0 },
  ],
  dji_model: 'm3e',
}
const summary = {
  id: 'route/id 1', name: '校园路线', description: '',
  created_at: '2026-10-14T00:00:00Z', updated_at: '2026-10-14T00:00:00Z', version: 1,
  anchor_count: 2,
  start: { latitude: 31.68, longitude: 119.95 },
  end: { latitude: 31.69, longitude: 119.96 },
}
const record = { ...summary, payload }

const signal = new AbortController().signal
responses.push(reply(200, { templates: [summary] }))
const listed = await api.listRouteTemplates(signal)
assert.equal(listed.length, 1)
assert.equal(listed[0].name, '校园路线')
assert.equal(listed[0].anchor_count, 2)
assert.deepEqual(listed[0].start, { latitude: 31.68, longitude: 119.95 })
assert.deepEqual(listed[0].end, { latitude: 31.69, longitude: 119.96 })
assert.equal(calls.at(-1).url, '/api/route-templates')
assert.equal(calls.at(-1).init.signal, signal)

responses.push(reply(200, record))
assert.equal((await api.getRouteTemplate(summary.id)).payload, payload)
assert.equal(calls.at(-1).url, '/api/route-templates/route%2Fid%201')

responses.push(reply(201, record))
assert.equal((await api.createRouteTemplate({ name: summary.name, payload })).id, summary.id)
assert.equal(calls.at(-1).init.method, 'POST')
assert.equal(calls.at(-1).init.headers['Content-Type'], 'application/json')
assert.deepEqual(JSON.parse(calls.at(-1).init.body).payload, payload)

responses.push(reply(200, { ...record, version: 2 }))
assert.equal((await api.updateRouteTemplate(summary.id, {
  name: summary.name, description: '第二版', payload, expected_version: 1,
})).version, 2)
assert.equal(calls.at(-1).init.method, 'PUT')
assert.equal(JSON.parse(calls.at(-1).init.body).expected_version, 1)

responses.push(reply(204))
await api.deleteRouteTemplate(summary.id)
assert.equal(calls.at(-1).init.method, 'DELETE')

responses.push(reply(409, { detail: '模板已被其他操作修改' }))
await assert.rejects(api.updateRouteTemplate(summary.id, {
  name: summary.name, payload, expected_version: 1,
}), error => error.name === 'RouteTemplateApiError' && error.status === 409 && /其他操作/.test(error.message))

responses.push(reply(500, null))
await assert.rejects(api.listRouteTemplates(), error => error.status === 500 && error.message.length > 0)

responses.push(reply(200, { templates: {} }))
await assert.rejects(api.listRouteTemplates(), error => error.status === 200 && /格式错误/.test(error.message))

console.log('Passed: route template list, load, create, update, delete, URL encoding, conflict, and malformed responses.')

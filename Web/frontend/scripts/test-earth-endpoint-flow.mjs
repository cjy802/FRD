import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import { parse } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as Vue from 'vue'

const source = await readFile(new URL('../src/App.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const requests = []
const reply = payload => ({ ok: true, json: async () => payload })
const context = vm.createContext({
  exports: {},
  require: name => name === 'vue'
    ? { ...Vue, onMounted() {}, onBeforeUnmount() {} }
    : {},
  AbortController, URLSearchParams, setTimeout, clearTimeout,
  window: { removeEventListener() {} },
  fetch: async (url, options) => {
    requests.push({ url, body: JSON.parse(options.body) })
    if (url === '/api/landing/check') return reply({ state: 'road_nearby', search_radius_m: 35, road_distance_m: 4 })
    if (url === '/api/routes/plan') {
      const { start, end } = JSON.parse(options.body)
      return reply({ geometry: { coordinates: [
        [start.longitude, start.latitude], [end.longitude, end.latitude],
      ] }, distance_km: 0.3, straight_distance_km: 0.25 })
    }
    throw new Error(`Unexpected request ${url}`)
  },
})
const compiled = ts.transpileModule(`${descriptor.scriptSetup.content}\nglobalThis.app = {
  form, mapMode, baseMap, earthMapMounted, preview, loading, error,
  chooseMapPoint, chooseBaseMap,
}`, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText
new vm.Script(compiled, { filename: 'App.vue' }).runInContext(context)
const app = context.app

await app.chooseBaseMap('earth')
assert.equal(app.baseMap.value, 'earth')
assert.equal(app.earthMapMounted.value, true)
assert.equal(requests.length, 0, 'Switching base maps must not request a new route')

const start = { longitude: 119.9505, latitude: 31.6798 }
await app.chooseMapPoint(start)
while (app.loading.value) await new Promise(resolve => setTimeout(resolve, 0))
assert.equal(app.form.start.longitude, start.longitude)
assert.equal(app.form.start.latitude, start.latitude)
assert.equal(requests[0].url, '/api/landing/check')
assert.deepEqual(requests[0].body.point, start)
assert.equal(requests[1].url, '/api/routes/plan')
assert.deepEqual(requests[1].body.start, start)
assert.ok(app.preview.value)

app.mapMode.value = 'end'
const end = { longitude: 119.9496, latitude: 31.6801 }
await app.chooseMapPoint(end)
while (app.loading.value) await new Promise(resolve => setTimeout(resolve, 0))
assert.equal(app.form.end.longitude, end.longitude)
assert.equal(app.form.end.latitude, end.latitude)
assert.deepEqual(requests[2].body.point, end)
assert.deepEqual(requests[3].body.end, end)
assert.equal(app.error.value, '')

const plannedRoute = app.preview.value
await app.chooseBaseMap('current')
assert.equal(app.baseMap.value, 'current')
assert.equal(app.preview.value, plannedRoute, 'Switching back must preserve the route')
assert.equal(requests.length, 4)
console.log('Passed: Earth A/B clicks reach landing checks and route planning; map switching preserves the route.')

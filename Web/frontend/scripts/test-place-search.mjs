import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import { parse } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as Vue from 'vue'

const transpile = source => ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText
const coordinatesSource = await readFile(new URL('../src/baiduCoordinates.ts', import.meta.url), 'utf8')
const coordContext = vm.createContext({ exports: {} })
new vm.Script(transpile(coordinatesSource)).runInContext(coordContext)
const candidate = { name: '测试学校', address: '常州市测试路', uid: 'school', coordinate_system: 'bd09', location: { lat: 31.683938, lng: 119.96101 } }
const requests = []
let apiResponse = { ok: true, json: async () => ({ results: [candidate, { name: '没有坐标' }] }) }
const placeContext = vm.createContext({ exports: {}, require: () => coordContext.exports,
  URLSearchParams, fetch: async (url, options) => { requests.push({ url, options }); return apiResponse },
})
new vm.Script(transpile(await readFile(new URL('../src/placeSearch.ts', import.meta.url), 'utf8'))).runInContext(placeContext)
const signal = new AbortController().signal
const found = await placeContext.exports.fetchPlaces('  测试学校  ', '常州市', signal)
assert.equal(found.length, 1)
assert.equal(new URL(requests[0].url, 'http://localhost').searchParams.get('query'), '测试学校')
assert.equal(requests[0].options.signal, signal)
assert.equal(requests[0].url.includes('ak='), false, 'Browser requests must not contain AK')
const point = placeContext.exports.placeToPoint(candidate)
const projected = coordContext.exports.wgs84ToBd09([point.longitude, point.latitude])
assert.ok(Math.abs(projected[0] - candidate.location.lng) < 0.000001)
assert.ok(Math.abs(projected[1] - candidate.location.lat) < 0.000001)
assert.notEqual(point.longitude, candidate.location.lng, 'Displayed coordinates must use WGS84')
assert.throws(() => placeContext.exports.placeToPoint({ ...candidate, location: { lat: 119.9, lng: 31.68 } }))
apiResponse = { ok: false, json: async () => ({ detail: '百度 AK 未开启此服务（240）。' }) }
await assert.rejects(placeContext.exports.fetchPlaces('测试学校', '常州市', signal), /240/)

const vue = { ...Vue, onMounted() {}, onBeforeUnmount() {} }
// Even a transport that ignores abort must not restore stale search results.
let resolveOld
const componentSource = await readFile(new URL('../src/PlaceSearch.vue', import.meta.url), 'utf8')
let searchResponse = new Promise(resolve => { resolveOld = resolve })
const componentContext = vm.createContext({ exports: {}, require: name => name === 'vue' ? vue : {
  fetchPlaces: () => searchResponse, placeToPoint: placeContext.exports.placeToPoint,
}, defineProps: () => ({ disabled: false }), defineEmits: () => () => {}, AbortController,
})
new vm.Script(transpile(`${parse(componentSource).descriptor.scriptSetup.content}\nglobalThis.search = { query, region, results, pending, message, search, coordinates };`)).runInContext(componentContext)
const component = componentContext.search
component.query.value = '旧查询'
const pendingOld = component.search()
component.query.value = '新查询'
resolveOld([candidate])
await pendingOld
assert.equal(component.results.value.length, 0)
searchResponse = Promise.resolve([candidate])
await component.search()
assert.equal(component.results.value.length, 1)
assert.equal(component.coordinates(candidate), `纬度 ${point.latitude.toFixed(6)}，经度 ${point.longitude.toFixed(6)}（WGS84）`)
console.log('Passed: private-key proxy, BD-09 conversion, coordinate display, errors, and stale search cancellation.')

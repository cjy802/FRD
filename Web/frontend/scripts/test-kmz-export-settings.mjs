import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import { parse } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as Vue from 'vue'

const source = await readFile(new URL('../src/App.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const requests = []
const link = { click() {}, remove() {} }
const context = vm.createContext({
  exports: {},
  require: name => name === 'vue' ? { ...Vue, watch() {}, onMounted() {}, onBeforeUnmount() {} } : {},
  fetch: async (url, options) => {
    requests.push({ url, body: JSON.parse(options.body) })
    return { ok: true, blob: async () => new Blob() }
  },
  URL: { createObjectURL: () => 'blob:kmz', revokeObjectURL() {} },
  document: { createElement: () => link, body: { appendChild() {} } },
  requestAnimationFrame: () => 1,
  window: { removeEventListener() {} },
  AbortController,
  URLSearchParams,
  setTimeout,
  clearTimeout,
})
const compiled = ts.transpileModule(`${descriptor.scriptSetup.content}\nglobalThis.app = {
  preview, waypointSettings, selectedWaypointIndex, markWaypointEdited,
  setSelectedWaypointOrientation, exportDjiKmz, kmzError,
}`, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText
new vm.Script(compiled, { filename: 'App.vue' }).runInContext(context)
const app = context.app
app.preview.value = { geometry: { coordinates: [[119.95, 31.67], [119.951, 31.671], [119.952, 31.672]] } }
app.waypointSettings.value = Array.from({ length: 3 }, () => ({
  altitude_m: 100, heading_mode: 'followWayline', heading_angle: 0, gimbal_pitch_angle: -30,
}))

app.selectedWaypointIndex.value = 1
app.setSelectedWaypointOrientation(315, -48)
assert.equal(app.waypointSettings.value[1].heading_mode, 'smoothTransition')
assert.equal(app.waypointSettings.value[1].heading_angle, 315)
assert.equal(app.waypointSettings.value[1].gimbal_pitch_angle, -48)
assert.equal(app.waypointSettings.value[1].export_anchor, true)

const rows = [{}, {}, {}]
rows[2].parentElement = { querySelectorAll: () => rows }
app.markWaypointEdited({ target: { closest: () => rows[2] } })
assert.equal(app.waypointSettings.value[2].export_anchor, true)
assert.equal(app.waypointSettings.value[0].export_anchor, undefined)

await app.exportDjiKmz()
assert.equal(app.kmzError.value, '')
assert.equal(requests.length, 1)
assert.equal(requests[0].url, '/api/routes/export-kmz')
assert.equal(requests[0].body.waypoint_settings[1].heading_mode, 'smoothTransition')
assert.equal(requests[0].body.waypoint_settings[1].gimbal_pitch_angle, -48)
assert.equal(requests[0].body.waypoint_settings[1].export_anchor, true)
assert.equal(requests[0].body.waypoint_settings[2].export_anchor, true)

console.log('Passed: edited heading, gimbal pitch, and protected waypoint settings reach KMZ export.')

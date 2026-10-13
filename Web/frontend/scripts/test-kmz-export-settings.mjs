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
  cameraPreviewElement, mapEditMode, rotateSelectedWaypoint,
  handleCameraPreviewPointerDown, handleCameraPreviewPointerMove, handleCameraPreviewPointerEnd,
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

app.waypointSettings.value[1] = {
  altitude_m: 100, heading_mode: 'followWayline', heading_angle: 0, gimbal_pitch_angle: -30,
}
app.mapEditMode.value = 'node'
app.cameraPreviewElement.value = {
  getBoundingClientRect: () => ({ width: 320, height: 200 }),
  setPointerCapture() {}, hasPointerCapture: () => false,
}
const pointer = extra => ({ button: 0, isPrimary: true, pointerId: 1, clientX: 100, clientY: 100, preventDefault() {}, ...extra })
app.handleCameraPreviewPointerDown(pointer())
app.handleCameraPreviewPointerMove(pointer({ clientX: 110, clientY: 160 }))
assert.equal(app.waypointSettings.value[1].heading_mode, 'followWayline', 'Vertical drag must preserve the heading mode')
assert.equal(app.waypointSettings.value[1].heading_angle, 0, 'Vertical drag must preserve the heading value')
assert.equal(app.waypointSettings.value[1].gimbal_pitch_angle, -57)
app.handleCameraPreviewPointerMove(pointer({ clientX: 180, clientY: 160 }))
assert.equal(app.waypointSettings.value[1].heading_mode, 'followWayline', 'A pitch drag must keep its axis even if the pointer drifts sideways')
assert.equal(app.waypointSettings.value[1].heading_angle, 0)
app.handleCameraPreviewPointerEnd(pointer())
app.handleCameraPreviewPointerDown(pointer())
app.handleCameraPreviewPointerMove(pointer({ clientX: 180, clientY: 105 }))
assert.equal(app.waypointSettings.value[1].heading_mode, 'smoothTransition', 'A horizontal drag may explicitly change yaw')
assert.equal(app.waypointSettings.value[1].gimbal_pitch_angle, -59.25)
app.handleCameraPreviewPointerEnd(pointer())
app.waypointSettings.value[1].heading_mode = 'followWayline'
app.waypointSettings.value[1].heading_angle = 0
app.rotateSelectedWaypoint(0, -5)
assert.equal(app.waypointSettings.value[1].heading_mode, 'followWayline', 'Pitch buttons must preserve heading')
assert.equal(app.waypointSettings.value[1].gimbal_pitch_angle, -64.25)
await app.exportDjiKmz()
assert.equal(requests[1].body.waypoint_settings[1].heading_mode, 'followWayline')
assert.equal(requests[1].body.waypoint_settings[1].heading_angle, 0)
assert.equal(requests[1].body.waypoint_settings[1].gimbal_pitch_angle, -64.25)

console.log('Passed: yaw/pitch export, pitch-only drag/buttons preserve heading, and horizontal drag changes yaw explicitly.')

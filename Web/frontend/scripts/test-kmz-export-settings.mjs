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
  preview, waypointSettings, selectedWaypointIndex, markWaypointEdited, syncWaypointSettings,
  setSelectedWaypointOrientation, exportDjiKmz, kmzError,
  cameraPreviewElement, mapEditMode, rotateSelectedWaypoint,
  handleCameraPreviewPointerDown, handleCameraPreviewPointerMove, handleCameraPreviewPointerEnd,
  anchors, previewKey, routeKey, batchBoundaryIds, batchHeadingInput, batchPitchInput,
  batchWaypointRange, batchRouteAnchorIndices, batchFeedback, toggleBatchBoundary,
  applyBatchCamera, clearBatchRange,
}`, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText
new vm.Script(compiled, { filename: 'App.vue' }).runInContext(context)
const app = context.app
app.preview.value = { geometry: { coordinates: [[119.95, 31.67], [119.951, 31.671], [119.952, 31.672]] } }
app.syncWaypointSettings(app.preview.value)
assert.deepEqual(Array.from(app.waypointSettings.value, setting => setting.gimbal_pitch_angle), [0, 0, 0], 'Unedited waypoints must start with a level camera')

app.selectedWaypointIndex.value = 1
app.setSelectedWaypointOrientation(315, -48)
assert.equal(app.waypointSettings.value[1].heading_mode, 'smoothTransition')
assert.equal(app.waypointSettings.value[1].heading_angle, 315)
assert.equal(app.waypointSettings.value[1].gimbal_pitch_angle, -48)
assert.equal(app.waypointSettings.value[1].export_anchor, true)

const rows = [{}, {}, { dataset: { waypointIndex: '2' } }]
app.markWaypointEdited({ target: { closest: () => rows[2] } })
assert.equal(app.waypointSettings.value[2].export_anchor, true)
assert.equal(app.waypointSettings.value[0].export_anchor, true, 'The start anchor must be preserved')

await app.exportDjiKmz()
assert.equal(app.kmzError.value, '')
assert.equal(requests.length, 1)
assert.equal(requests[0].url, '/api/routes/export-kmz')
assert.equal(requests[0].body.waypoint_settings[1].heading_mode, 'smoothTransition')
assert.equal(requests[0].body.waypoint_settings[1].gimbal_pitch_angle, -48)
assert.deepEqual(requests[0].body.waypoint_settings.map(setting => setting.gimbal_pitch_angle), [0, -48, 0])
assert.equal(requests[0].body.waypoint_settings[1].export_anchor, true)
assert.equal(requests[0].body.waypoint_settings[2].export_anchor, true)
app.syncWaypointSettings(app.preview.value)
assert.equal(app.waypointSettings.value[1].gimbal_pitch_angle, -48, 'Route synchronization must preserve an explicitly edited pitch')

app.waypointSettings.value[1] = {
  altitude_m: 100, heading_mode: 'followWayline', heading_angle: 0, gimbal_pitch_angle: 0,
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
assert.equal(app.waypointSettings.value[1].gimbal_pitch_angle, -27)
app.handleCameraPreviewPointerMove(pointer({ clientX: 180, clientY: 160 }))
assert.equal(app.waypointSettings.value[1].heading_mode, 'followWayline', 'A pitch drag must keep its axis even if the pointer drifts sideways')
assert.equal(app.waypointSettings.value[1].heading_angle, 0)
app.handleCameraPreviewPointerEnd(pointer())
app.handleCameraPreviewPointerDown(pointer())
app.handleCameraPreviewPointerMove(pointer({ clientX: 180, clientY: 105 }))
assert.equal(app.waypointSettings.value[1].heading_mode, 'smoothTransition', 'A horizontal drag may explicitly change yaw')
assert.equal(app.waypointSettings.value[1].gimbal_pitch_angle, -29.25)
app.handleCameraPreviewPointerEnd(pointer())
app.waypointSettings.value[1].heading_mode = 'followWayline'
app.waypointSettings.value[1].heading_angle = 0
app.rotateSelectedWaypoint(0, -5)
assert.equal(app.waypointSettings.value[1].heading_mode, 'followWayline', 'Pitch buttons must preserve heading')
assert.equal(app.waypointSettings.value[1].gimbal_pitch_angle, -34.25)
await app.exportDjiKmz()
assert.equal(requests[1].body.waypoint_settings[1].heading_mode, 'followWayline')
assert.equal(requests[1].body.waypoint_settings[1].heading_angle, 0)
assert.equal(requests[1].body.waypoint_settings[1].gimbal_pitch_angle, -34.25)

app.anchors.value = Array.from({ length: 4 }, (_, index) => ({
  id: `anchor-${index + 1}`, latitude: 31.68 + index * 0.001, longitude: 119.96 + index * 0.001,
}))
const mission = {
  geometry: { coordinates: Array.from({ length: 7 }, (_, index) => [119.96 + index * 0.0005, 31.68 + index * 0.0005]) },
  anchors: app.anchors.value.map(anchor => ({ ...anchor })), anchor_indices: [0, 2, 4, 6],
}
app.syncWaypointSettings(mission)
app.preview.value = mission
app.previewKey.value = app.routeKey.value
assert.deepEqual(Array.from(app.batchRouteAnchorIndices.value), [0, 2, 4, 6])

app.toggleBatchBoundary('anchor-4')
const beforeSecondBoundary = JSON.stringify(app.waypointSettings.value)
app.batchPitchInput.value = '-35'
app.applyBatchCamera()
assert.equal(app.batchWaypointRange.value, null, 'One selected boundary must not activate batch edits')
assert.equal(JSON.stringify(app.waypointSettings.value), beforeSecondBoundary)

app.toggleBatchBoundary('anchor-2')
assert.deepEqual({ ...app.batchWaypointRange.value }, { start: 2, end: 6, count: 5, from: 'B', to: 'D' })
app.toggleBatchBoundary('anchor-1')
assert.deepEqual(Array.from(app.batchBoundaryIds.value), ['anchor-4', 'anchor-2'], 'A third boundary must be ignored')
app.waypointSettings.value[3].heading_mode = 'fixed'
app.waypointSettings.value[3].heading_angle = 17
app.waypointSettings.value[3].export_anchor = true
app.applyBatchCamera()
assert.deepEqual(Array.from(app.waypointSettings.value, setting => setting.gimbal_pitch_angle), [0, 0, -35, -35, -35, -35, -35])
assert.equal(app.waypointSettings.value[3].heading_angle, 17, 'Pitch-only edits must preserve the original heading')
assert.equal(app.waypointSettings.value[5].export_anchor, false, 'Generated points must remain eligible for KMZ simplification')

app.batchHeadingInput.value = '72'
app.batchPitchInput.value = ''
app.applyBatchCamera()
assert.deepEqual(Array.from(app.waypointSettings.value, setting => setting.heading_mode),
  ['followWayline', 'followWayline', 'fixed', 'fixed', 'fixed', 'fixed', 'fixed'])
assert.deepEqual(Array.from(app.waypointSettings.value, setting => setting.heading_angle), [0, 0, 72, 72, 72, 72, 72])
assert.deepEqual(Array.from(app.waypointSettings.value, setting => setting.gimbal_pitch_angle), [0, 0, -35, -35, -35, -35, -35])
await app.exportDjiKmz()
assert.deepEqual(requests.at(-1).body.waypoint_settings.map(setting => setting.heading_angle), [0, 0, 72, 72, 72, 72, 72])

const applied = JSON.stringify(app.waypointSettings.value)
app.batchHeadingInput.value = 'bad angle'
app.applyBatchCamera()
assert.equal(app.batchFeedback.value.error, true)
assert.equal(JSON.stringify(app.waypointSettings.value), applied, 'Invalid input must not change any point')
app.anchors.value[1].longitude += 0.001
assert.equal(app.batchWaypointRange.value, null, 'A changed route must disable stale range edits')
app.clearBatchRange()
assert.equal(app.batchBoundaryIds.value.length, 0)

console.log('Passed: per-waypoint camera export and inclusive two-boundary batch camera edits.')

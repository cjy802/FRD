import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import { parse } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as Vue from 'vue'

const source = await readFile(new URL('../src/App.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const calls = { camera: [], focus: [], mode: [], redraw: [], preview: 0 }
const viewer = { camera: { setView: pose => calls.camera.push(pose), flyTo: pose => calls.camera.push(pose) },
  scene: { morphTo2D: () => calls.mode.push('2d'), morphTo3D: () => calls.mode.push('3d'), requestRender() {} }, resize() {} }
const cesium = { Math: { toRadians: degrees => degrees * Math.PI / 180 }, Cartesian3: { fromDegrees: (...p) => p } }
const context = vm.createContext({ exports: {}, require: name => name === 'vue' ? { ...Vue, onMounted() {}, onBeforeUnmount() {} } : {},
  window: { removeEventListener() {} }, AbortController, URLSearchParams, setTimeout, clearTimeout, viewer, cesium, calls })
new vm.Script(ts.transpileModule(`${descriptor.scriptSetup.content}\nglobalThis.app = {
  preview, waypointSettings, selectedWaypointIndex, baseMap, mapViewMode, mapEditMode, earthRoute, selectedCameraPose,
  selectWaypoint, chooseBaseMap, chooseMapViewMode, updateCameraPreview, focusSelectedWaypoint,
  initialize() {
    Cesium = cesium; viewer = globalThis.viewer;
    earthMapRef.value = { focusWaypoint: index => calls.focus.push(index) };
    drawCesiumRoute = fit => calls.redraw.push(fit);
    initCameraPreview = () => calls.preview++;
  },
}`, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText).runInContext(context)
const app = context.app
app.initialize()
app.preview.value = { geometry: { coordinates: [[119.95, 31.68], [119.951, 31.681]] } }
app.waypointSettings.value = [{ altitude_m: 100, heading_mode: 'fixed', heading_angle: 80, gimbal_pitch_angle: -30 },
  { altitude_m: 180, heading_mode: 'fixed', heading_angle: 120, gimbal_pitch_angle: -45 }]
await Vue.nextTick()
const route = app.preview.value
await app.chooseBaseMap('earth'); app.selectWaypoint(1)
assert.deepEqual(calls.focus, [1]); assert.equal(calls.camera.length, 0, 'Earth selection must not move the hidden Cesium map')
assert.equal(calls.preview, 0, 'Earth preview must not initialize the old basemap renderer')
assert.equal(app.earthRoute.value[1][2], 180)
assert.equal(app.selectedCameraPose.value.altitude, 180); assert.equal(app.selectedCameraPose.value.heading, 120)
app.waypointSettings.value[1].gimbal_pitch_angle = -20
assert.equal(app.selectedCameraPose.value.pitch, -20, 'Both preview renderers consume the same reactive camera pose')
app.chooseMapViewMode('2d')
assert.equal(calls.mode.length, 0, 'Earth mode switches must not morph the hidden Cesium view')
await app.chooseBaseMap('current')
assert.equal(calls.mode.at(-1), '2d'); assert.equal(app.preview.value, route)
app.focusSelectedWaypoint()
assert.equal(calls.camera.at(-1).orientation.heading, 0)
assert.equal(calls.camera.at(-1).orientation.pitch, -Math.PI / 2, 'Selecting a node in 2D must keep a north-up overhead view')
app.chooseMapViewMode('3d'); assert.equal(calls.mode.at(-1), '3d')
assert.equal(app.waypointSettings.value[1].altitude_m, 180)
assert.equal(app.selectedWaypointIndex.value, 1)
console.log('Passed: focus follows the active map, north-up 2D node editing, shared preview pose, route/selection/height preservation.')

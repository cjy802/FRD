import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import { parse } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as Vue from 'vue'

// Exercise the component's actual input handlers and reactive selection changes.
// Replace only Cesium rendering so this regression test runs without WebGL.
const source = await readFile(process.env.CAMERA_COMPONENT_SOURCE || new URL('../src/App.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const cleanup = []
const frames = new Map()
let frameId = 0
const frustumUpdates = []
const camera = {
  position: null,
  orientation: null,
  frustum: {},
  setView({ destination, orientation }) {
    if (destination) this.position = { ...destination }
    this.orientation = { ...orientation }
  },
}
const previewController = {
  enableRotate: true, enableTranslate: true, enableZoom: true, enableTilt: true, enableLook: true,
  inertiaSpin: 0.9, inertiaTranslate: 0.9, inertiaZoom: 0.8,
}
const removedActions = []
const captures = new Set()
const previewElement = {
  getBoundingClientRect: () => ({ width: 320, height: 200 }),
  setPointerCapture: id => captures.add(id),
  hasPointerCapture: id => captures.has(id),
  releasePointerCapture: id => captures.delete(id),
}
const mainController = { enableInputs: true }
const mainViewer = { scene: { screenSpaceCameraController: mainController }, isDestroyed: () => true }
const cesium = {
  Math: { toRadians: degrees => degrees * Math.PI / 180 },
  Cartesian3: { fromDegrees: (longitude, latitude, height) => ({ x: longitude, y: latitude, z: height }) },
  ScreenSpaceEventType: { LEFT_DOUBLE_CLICK: 'double-click' },
  EllipsoidTerrainProvider: class {},
  OpenStreetMapImageryProvider: class {},
  Viewer: class {
    camera = camera
    scene = { globe: {}, screenSpaceCameraController: previewController, requestRender() {} }
    imageryLayers = { addImageryProvider() {} }
    cesiumWidget = { screenSpaceEventHandler: { removeInputAction: action => removedActions.push(action) } }
    resize() {}
    isDestroyed() { return true }
  },
}
const vue = { ...Vue, onMounted() {}, onBeforeUnmount: callback => cleanup.push(callback) }
const context = vm.createContext({
  exports: {},
  require: name => name === 'vue' ? vue : {},
  Date, AbortController, URLSearchParams, setTimeout, clearTimeout,
  window: { removeEventListener() {} },
  requestAnimationFrame: callback => { const id = ++frameId; frames.set(id, callback); return id },
  cancelAnimationFrame: id => frames.delete(id),
  cesium, previewElement, mainViewer, frustumUpdates,
})
const compiled = ts.transpileModule(`${descriptor.scriptSetup.content}\n
globalThis.app = {
  preview, waypointSettings, selectedWaypointIndex, mapEditMode, draggingCameraIndex,
  waypointHeading, rotateSelectedWaypoint, handleWaypointKeydown, updateCameraPreview,
  handleCameraPreviewPointerDown, handleCameraPreviewPointerMove, handleCameraPreviewPointerEnd,
  cancelCameraPreviewDrag, finishMapCameraDrag,
  initialize() {
    Cesium = cesium
    viewer = mainViewer
    cameraPreviewElement.value = previewElement
    drawCesiumRoute = () => {}
    updateSelectedWaypointView = () => { frustumUpdates.push(waypointHeading(selectedWaypointIndex.value)) }
    initCameraPreview()
  },
  startMapDrag(previousInputs) {
    draggingCameraIndex.value = selectedWaypointIndex.value
    mapCameraInputsBeforeDrag = previousInputs
    viewer.scene.screenSpaceCameraController.enableInputs = false
  },
}`, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText
new vm.Script(compiled, { filename: 'App.vue' }).runInContext(context)
const app = context.app
app.initialize()
app.preview.value = { geometry: { coordinates: [[119.95, 31.68], [119.951, 31.681]] } }
app.waypointSettings.value = [
  { altitude_m: 100, heading_mode: 'followWayline', heading_angle: 0, gimbal_pitch_angle: -30 },
  { altitude_m: 120, heading_mode: 'fixed', heading_angle: 180, gimbal_pitch_angle: -20 },
]
app.selectedWaypointIndex.value = 0
app.mapEditMode.value = 'node'
await Vue.nextTick()
app.updateCameraPreview()
const coordinates = JSON.stringify(app.preview.value.geometry.coordinates)
const position = { ...camera.position }
const normalize = degrees => ((degrees % 360) + 360) % 360
const close = (actual, expected) => assert.ok(Math.abs(actual - expected) < 1e-9, `${actual} != ${expected}`)
const flush = () => {
  const callbacks = [...frames.values()]
  frames.clear()
  callbacks.forEach(callback => callback())
}
const assertFixedPosition = () => {
  flush()
  assert.equal(JSON.stringify(app.preview.value.geometry.coordinates), coordinates, 'Rotation must preserve every waypoint position')
  assert.equal(app.waypointSettings.value[0].altitude_m, 100, 'Rotation must preserve height')
  assert.deepEqual(camera.position, position, 'The preview camera must stay at the same position')
  close(camera.orientation.heading, app.waypointHeading(0) * Math.PI / 180)
  close(camera.orientation.pitch, app.waypointSettings.value[0].gimbal_pitch_angle * Math.PI / 180)
}
const pointer = (extra = {}) => ({ button: 0, isPrimary: true, pointerId: 1, clientX: 100, clientY: 100, preventDefault() {}, ...extra })

assert.equal(Object.values(previewController).some(Boolean), false, 'Disable all default camera motion and inertia')
assert.deepEqual(removedActions, ['double-click'], 'Double-click must not trigger automatic camera movement')
const routeHeading = app.waypointHeading(0)
assert.ok(routeHeading > 1, 'Use a route whose heading differs from the default zero setting')
app.handleWaypointKeydown({ key: 'e', target: null, preventDefault() {} })
close(app.waypointHeading(0), normalize(routeHeading + 5))
assertFixedPosition()
for (let i = 0; i < 80; i++) app.rotateSelectedWaypoint(15)
assert.ok(app.waypointHeading(0) >= 0 && app.waypointHeading(0) < 360)
assertFixedPosition()
app.handleWaypointKeydown({ key: 'q', target: null, preventDefault() {} })
assertFixedPosition()
app.rotateSelectedWaypoint(0, 200)
assert.equal(app.waypointSettings.value[0].gimbal_pitch_angle, 45)
assertFixedPosition()
app.rotateSelectedWaypoint(0, -300)
assert.equal(app.waypointSettings.value[0].gimbal_pitch_angle, -90)
assertFixedPosition()
app.rotateSelectedWaypoint(0, 60)
flush()

const beforeDrag = app.waypointHeading(0)
app.handleCameraPreviewPointerDown(pointer())
assert.ok(captures.has(1), 'Capture the pointer so dragging continues outside the preview')
app.handleCameraPreviewPointerMove(pointer({ clientX: 180, clientY: 60 }))
close(app.waypointHeading(0), normalize(beforeDrag + 45))
close(app.waypointSettings.value[0].gimbal_pitch_angle, -12)
assert.equal(frames.size, 0, 'Dragging must refresh the frustum immediately instead of waiting for the next frame batch')
close(frustumUpdates.at(-1), app.waypointHeading(0))
assert.ok(captures.has(1), 'The updated frustum must be visible while the pointer is still held')
assertFixedPosition()
app.handleCameraPreviewPointerMove(pointer({ pointerId: 2, clientX: 300 }))
close(app.waypointHeading(0), normalize(beforeDrag + 45))
app.handleCameraPreviewPointerEnd(pointer())
assert.equal(captures.size, 0)
const afterRelease = app.waypointHeading(0)
app.handleCameraPreviewPointerMove(pointer({ clientX: 300 }))
close(app.waypointHeading(0), afterRelease)
assertFixedPosition()

app.handleCameraPreviewPointerDown(pointer({ button: 2 }))
assert.equal(captures.size, 0, 'Ignore secondary mouse buttons')
app.handleCameraPreviewPointerDown(pointer({ isPrimary: false }))
assert.equal(captures.size, 0, 'Ignore additional touch pointers')
app.handleCameraPreviewPointerDown(pointer())
app.handleCameraPreviewPointerMove(pointer({ clientY: 2000 }))
assert.equal(app.waypointSettings.value[0].gimbal_pitch_angle, -90)
assertFixedPosition()
app.cancelCameraPreviewDrag()
assert.equal(captures.size, 0)

app.handleCameraPreviewPointerDown(pointer())
app.selectedWaypointIndex.value = 1
assert.equal(captures.size, 0, 'Changing the selection must cancel a captured drag immediately')
app.handleCameraPreviewPointerMove(pointer({ clientX: 300 }))
assert.equal(app.waypointSettings.value[1].heading_angle, 180, 'A stale drag must not rotate the new selection')
app.selectedWaypointIndex.value = 0
await Vue.nextTick()
assertFixedPosition()

for (const previousInputs of [true, false]) {
  app.startMapDrag(previousInputs)
  app.finishMapCameraDrag()
  assert.equal(app.draggingCameraIndex.value, null)
  assert.equal(mainController.enableInputs, previousInputs, 'Restore the previous main-map input state')
}
app.handleCameraPreviewPointerDown(pointer())
app.mapEditMode.value = 'route'
assert.equal(captures.size, 0, 'Leaving node mode must cancel preview dragging')
app.mapEditMode.value = 'node'
await Vue.nextTick()
app.handleCameraPreviewPointerDown(pointer())
app.startMapDrag(true)
cleanup.forEach(callback => callback())
assert.equal(captures.size, 0, 'Unmount must release pointer capture')
assert.equal(mainController.enableInputs, true, 'Unmount must restore main-map inputs')

console.log('Passed: fixed camera coordinates/height, actual initial heading, keyboard/buttons/preview drag, angle limits, pointer cancellation, selection changes, and input restoration.')

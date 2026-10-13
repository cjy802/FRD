import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import { parse } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as Vue from 'vue'

const source = await readFile(new URL('../src/App.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const entities = []
let renderRequests = 0
const color = value => ({ value, withAlpha: alpha => color(`${value}@${alpha}`) })
const cesium = {
  Cartesian3: {
    fromDegrees: (longitude, latitude, height) => ({ longitude, latitude, height }),
    fromDegreesArray: values => values,
    fromDegreesArrayHeights: values => values,
  },
  Cartesian2: class { constructor(x, y) { this.x = x; this.y = y } },
  Color: { fromCssColorString: color, WHITE: color('white') },
  HeightReference: { CLAMP_TO_GROUND: 'clamp' },
  VerticalOrigin: { CENTER: 'center' },
  HorizontalOrigin: { CENTER: 'center' },
  LabelStyle: { FILL_AND_OUTLINE: 'fill-and-outline' },
  ArcType: { NONE: 'none' },
  Math: { toRadians: degrees => degrees * Math.PI / 180, toDegrees: radians => radians * 180 / Math.PI },
  Cartographic: { fromCartesian: position => ({ longitude: position.longitude * Math.PI / 180,
    latitude: position.latitude * Math.PI / 180, height: position.height }) },
  PolylineGlowMaterialProperty: class { constructor(options) { Object.assign(this, options) } },
  PolylineDashMaterialProperty: class { constructor(options) { Object.assign(this, options) } },
  CallbackProperty: class { constructor(callback) { this.getValue = callback } },
  PolygonHierarchy: class { constructor(positions) { this.positions = positions } },
}
const viewer = {
  entities: {
    add(entity) { entities.push(entity); return entity },
    removeAll() { entities.length = 0 },
    getById(id) { return entities.find(entity => entity.id === id) },
  },
  camera: { flyTo() {} },
  scene: { requestRender() { renderRequests++ } },
}
const vue = { ...Vue, onMounted() {}, onBeforeUnmount() {} }
const context = vm.createContext({
  exports: {},
  require: name => name === 'vue' ? vue : {},
  window: { removeEventListener() {} },
  Cesium: cesium,
  AbortController,
  URLSearchParams,
  setTimeout,
  clearTimeout,
})
const compiled = ts.transpileModule(`${descriptor.scriptSetup.content}\nglobalThis.app = {
  preview, previewKey, routeKey, routeAssessment, waypointSettings,
  selectedWaypointIndex, mapEditMode, earthFootprint, drawCesiumRoute, setSelectedWaypointOrientation,
  initialize() { Cesium = globalThis.Cesium; viewer = globalThis.mockViewer; updateCameraPreview = () => {} },
}`, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText
context.mockViewer = viewer
new vm.Script(compiled, { filename: 'App.vue' }).runInContext(context)
const app = context.app
app.initialize()

const coordinates = [
  [119.9515, 31.6792],
  [119.9512, 31.6791],
  [119.9504, 31.6791], // W03
  [119.9500, 31.6791],
  [119.9494, 31.6791], // W05
  [119.9494, 31.6795],
]
app.preview.value = { geometry: { coordinates }, straight_distance_km: 0.4 }
app.previewKey.value = app.routeKey.value
app.waypointSettings.value = coordinates.map(() => ({ altitude_m: 100, heading_mode: 'fixed', heading_angle: 0, gimbal_pitch_angle: -30 }))
app.routeAssessment.value = {
  weather_risk: { segments: [{ index: 0, start: coordinates[2], end: coordinates[4], level: 'attention' }] },
}
app.mapEditMode.value = 'node'
app.selectedWaypointIndex.value = 2
app.drawCesiumRoute(false)

const selected = () => entities.filter(entity => entity.point?.outlineColor?.value === '#ffd54f')
assert.equal(selected().length, 1, 'Exactly one elevated waypoint should have a yellow selection ring')
assert.equal(selected()[0].properties.waypointIndex, 2)
assert.deepEqual({ ...selected()[0].position }, { longitude: coordinates[2][0], latitude: coordinates[2][1], height: 100 })
assert.equal(selected()[0].point.show, true)
assert.equal(selected()[0].point.outlineWidth, 4)
assert.equal(entities.some(entity => entity.id === 'selected-waypoint-ground'), false)
assert.equal(entities.find(entity => entity.name === '地面航点 5').point.color.value, '#00e5a8@0.88')
assert.equal(entities.filter(entity => entity.name?.startsWith('气象初筛地面点')).length, 0)

const face = entities.find(entity => entity.id === 'selected-view-face-0').polygon.hierarchy
const direction = entities.find(entity => entity.id === 'selected-view-direction').polyline.positions
const border = entities.find(entity => entity.id === 'selected-view-border').polyline.positions
const origin = { ...direction.getValue()[0] }
const beforeFace = JSON.stringify(face.getValue().positions)
const beforeBorder = JSON.stringify(border.getValue())
const requestsBeforeDrag = renderRequests
app.setSelectedWaypointOrientation(90, undefined, true)
assert.notEqual(JSON.stringify(face.getValue().positions), beforeFace, 'The green face must expose new vertices during dragging')
assert.notEqual(JSON.stringify(border.getValue()), beforeBorder, 'The green border must follow the face')
assert.deepEqual({ ...direction.getValue()[0] }, origin, 'Rotation must keep the camera origin fixed')
assert.equal(renderRequests, requestsBeforeDrag + 1, 'Each drag update must request a map render')
app.setSelectedWaypointOrientation(90, 0, true)
const horizontalCorners = app.earthFootprint.value
assert.equal(horizontalCorners.length, 4)
assert.ok(horizontalCorners.slice(0, 2).every(corner => corner[2] > 100), 'Horizontal upper corners must retain their altitude for Earth rendering')
assert.ok(horizontalCorners.slice(2).every(corner => corner[2] < 2), 'Camera perimeter order must keep the lower corners adjacent')
app.setSelectedWaypointOrientation(90, -90, true)
assert.ok(app.earthFootprint.value.every(corner => Math.abs(corner[2]) < 1e-6), 'A nadir view intersects the ground at all four corners')

app.selectedWaypointIndex.value = 4
app.drawCesiumRoute(false)
assert.equal(selected().length, 1)
assert.equal(selected()[0].properties.waypointIndex, 4)
assert.deepEqual({ ...selected()[0].position }, { longitude: coordinates[4][0], latitude: coordinates[4][1], height: 100 })

app.mapEditMode.value = 'route'
app.drawCesiumRoute(false)
assert.equal(selected().length, 0)
assert.equal(entities.filter(entity => entity.name?.startsWith('气象初筛地面点')).length, 2)

console.log('Passed: live frustum, fixed camera origin, one yellow ring on the selected elevated waypoint, and weather overlay separation.')

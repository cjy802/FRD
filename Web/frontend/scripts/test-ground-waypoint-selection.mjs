import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import { parse } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as Vue from 'vue'

const source = await readFile(new URL('../src/App.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const entities = []
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
  Math: { toRadians: degrees => degrees * Math.PI / 180 },
  PolylineGlowMaterialProperty: class { constructor(options) { Object.assign(this, options) } },
  PolylineDashMaterialProperty: class { constructor(options) { Object.assign(this, options) } },
}
const viewer = {
  entities: {
    add(entity) { entities.push(entity); return entity },
    removeAll() { entities.length = 0 },
  },
  camera: { flyTo() {} },
  scene: { requestRender() {} },
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
  selectedWaypointIndex, mapEditMode, drawCesiumRoute,
  initialize() { Cesium = globalThis.Cesium; viewer = globalThis.mockViewer },
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

const selected = () => entities.filter(entity => entity.id === 'selected-waypoint-ground')
assert.equal(selected().length, 1, 'Exactly one selected ground marker should be drawn')
assert.equal(selected()[0].properties.waypointIndex, 2)
assert.deepEqual({ ...selected()[0].position }, { longitude: coordinates[2][0], latitude: coordinates[2][1], height: 0 })
assert.equal(selected()[0].point.outlineColor.value, '#ffd54f')
assert.equal(selected()[0].point.heightReference, 'clamp')
assert.equal(entities.find(entity => entity.name === '地面航点 5').point.color.value, '#00e5a8@0.88')
assert.equal(entities.filter(entity => entity.name?.startsWith('气象初筛地面点')).length, 0)

app.selectedWaypointIndex.value = 4
app.drawCesiumRoute(false)
assert.equal(selected().length, 1)
assert.equal(selected()[0].properties.waypointIndex, 4)
assert.deepEqual({ ...selected()[0].position }, { longitude: coordinates[4][0], latitude: coordinates[4][1], height: 0 })

app.mapEditMode.value = 'route'
app.drawCesiumRoute(false)
assert.equal(selected().length, 0)
assert.equal(entities.filter(entity => entity.name?.startsWith('气象初筛地面点')).length, 2)

console.log('Passed: W03/W05 ground projection selection and weather overlay separation.')

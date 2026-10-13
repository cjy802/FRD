import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import ts from 'typescript'
import { PerspectiveCamera, Vector3 } from 'three'

const source = (await readFile(new URL('../src/baiduScene.ts', import.meta.url), 'utf8')).replaceAll('import.meta.env', 'globalThis.environment')
const context = vm.createContext({ exports: {}, require() {}, environment: {}, window: {} })
new vm.Script(ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText).runInContext(context)
const camera = new PerspectiveCamera(58, 4 / 3, .1, 20000000)
camera.position.set(0, 0, 6378137 * 3)
camera.lookAt(0, 0, 0); camera.updateMatrixWorld()
const layers = new Set(), poses = [], projections = []
const engine = { camera, controller: {}, rendering: {},
  map: { isGlobe: false, projectArrayCoordinate: (p, output) => { output.push(...p); return output }, unprojectArrayCoordinate: p => p,
    setProjection: p => projections.push(p), lookAt: (p, options) => poses.push({ p: [...p], ...options }),
    getHeading: () => 35, getPitch: () => 60, setPitch() {}, pickSeaLevelWorldPosition: () => new Vector3(119.95, 31.68, 1),
    _map: { setView: (p, options) => poses.push({ p: [...p], ...options }) } },
  add: layer => layers.add(layer), remove: layer => layers.delete(layer), requestRender() {},
  addBeforeRenderListener() {}, removeBeforeRenderListener() {}, dispose() {},
}
class Layer {
  constructor(options) { this.options = options }
  set dataSource(value) {
    assert.ok(layers.has(this), 'Bind geometry only after the SDK layer has an engine and projection')
    this.source = value
  }
  get dataSource() { return this.source }
}
const sdk = { Polyline: Layer, Polygon: Layer, GeoJSONDataSource: { fromGeoJSON: json => json } }
const scene = new context.exports.BaiduScene(engine, sdk, { getBoundingClientRect: () => ({ width: 800, height: 600 }) })
const route = [[119.95, 31.68, 100], [119.951, 31.681, 180]]
const weather = [{ start: route[0], end: route[1], level: 'no_trigger' }]
const original = JSON.stringify(route)
const coordinates = layer => layer.dataSource.features[0].geometry.coordinates
scene.setRoute(route, '3d', weather, [])
const core = [...layers].find(layer => layer.options.lineWidth === 4)
assert.equal(coordinates(core)[1][2], 180, 'The actual render data retains unequal waypoint heights')
const outline = [...layers].find(layer => layer.options.lineWidth === 9)
assert.equal(coordinates(outline)[0][2], 99.5, 'The dark outline must sit behind the green line')
assert.equal(coordinates([...layers].find(layer => layer.options.color === '#0f6cbd'))[0][2], 2, 'Weather belongs on the ground in 3D')
const oldLayers = [...layers]
scene.setView('2d', route); scene.setRoute(route, '2d', weather, [])
assert.equal(projections.at(-1), 'EPSG:3857')
assert.equal(poses.at(-1).heading, 0); assert.equal(poses.at(-1).pitch, 0)
assert.equal(coordinates([...layers].find(layer => layer.options.lineWidth === 4))[1][2], 3)
const weatherLine = [...layers].find(layer => layer.options.color === '#0f6cbd')
assert.equal(coordinates(weatherLine)[0][2], 2, '2D weather must remain below the green line')
assert.equal(weatherLine.options.lineWidth, 13, 'The weather halo must stay visible beyond the dark outline')
assert.deepEqual(coordinates(weatherLine)[0].slice(0, 2), coordinates([...layers].find(layer => layer.options.lineWidth === 4))[0].slice(0, 2), 'Weather must retain its actual geographic position')
assert.ok(oldLayers.every(layer => !layers.has(layer)), 'Switching replaces obsolete geometry instead of leaving duplicate layers')
scene.setView('3d', route); scene.setRoute(route, '3d', weather, [])
assert.equal(projections.at(-1), 'ecef'); assert.equal(poses.at(-1).pitch, 52)
assert.equal(JSON.stringify(route), original)
scene.setFootprint(route[1], [[119.95, 31.68], [119.951, 31.68], [119.951, 31.681]], '3d')
assert.ok([...layers].some(layer => coordinates(layer)?.[0]?.[2] === 180), 'Frustum rays originate at the selected elevated camera')
const pose = { longitude: 119.95, latitude: 31.68, altitude: 100, heading: 45, pitch: -30 }
scene.setPose(pose); const first = poses.at(-1)
scene.setPose({ ...pose, heading: 90, pitch: 15 }); const second = poses.at(-1)
assert.deepEqual(first.p, second.p); assert.equal(second.range, undefined)
assert.equal(first.heading, 315); assert.equal(first.pitch, 60)
assert.equal(second.heading, 270); assert.equal(second.pitch, 105)
assert.equal(scene.project([0, 0, 0])?.x, 400)
assert.equal(scene.project([0, 0, camera.position.z + 100]), null, 'Nodes behind the camera cannot be clicked')
engine.map.isGlobe = true
assert.equal(scene.project([0, 0, -6356752]), null, 'Nodes on the far side of Earth must be hidden')
assert.equal(scene.project([0, 0, 6356754])?.x, 400)
scene.dispose(); assert.equal(layers.size, 0)
console.log('Passed: elevated render geometry, north-up 2D, globe projection, fixed preview pose, camera clipping and Earth occlusion.')

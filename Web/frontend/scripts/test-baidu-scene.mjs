import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import ts from 'typescript'
import * as Three from 'three'
const { PerspectiveCamera, Vector3, DoubleSide } = Three

const source = (await readFile(new URL('../src/baiduScene.ts', import.meta.url), 'utf8')).replaceAll('import.meta.env', 'globalThis.environment')
const coordinateContext = vm.createContext({ exports: {} })
const coordinateSource = await readFile(new URL('../src/baiduCoordinates.ts', import.meta.url), 'utf8')
new vm.Script(ts.transpileModule(coordinateSource, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText).runInContext(coordinateContext)
const { toBaiduSceneCoordinate, fromBaiduSceneCoordinate } = coordinateContext.exports
const context = vm.createContext({ exports: {}, require: name => name === './baiduCoordinates' ? coordinateContext.exports : name === 'three' ? Three : {}, environment: {}, window: {} })
new vm.Script(ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText).runInContext(context)
const camera = new PerspectiveCamera(58, 4 / 3, .1, 20000000)
camera.position.set(0, 0, 6378137 * 3)
camera.lookAt(0, 0, 0); camera.updateMatrixWorld()
const layers = new Set(), poses = [], projections = []
const engine = { camera, controller: {}, rendering: {},
  map: { isGlobe: false, projectArrayCoordinate: (p, output) => { output.push(...p); return output }, unprojectArrayCoordinate: p => p,
    setProjection: p => projections.push(p), lookAt: (p, options) => poses.push({ p: [...p], ...options }),
    getHeading: () => 35, getPitch: () => 60, setPitch() {}, pickSeaLevelWorldPosition: () => new Vector3(119.95, 31.68, 1),
    _map: { setView: (p, options) => poses.push({ p: [...p], ...options }), controller: { enableInputs: true } } },
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
const coordinates = layer => layer.dataSource?.features[0].geometry.coordinates
scene.setRoute(route, '3d', weather, [])
const core = [...layers].find(layer => layer.options.lineWidth === 4)
assert.equal(coordinates(core)[1][2], 180, 'The actual render data retains unequal waypoint heights')
assert.deepEqual(coordinates(core)[1], toBaiduSceneCoordinate(route[1]), 'Route geometry must use the satellite imagery datum')
assert.ok(coordinates(core)[0][0] - route[0][0] > .005, 'Campus geometry must receive the missing BD-09 conversion')
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
assert.ok(Math.abs(poses.at(-1).p[0] - toBaiduSceneCoordinate([119.9505, 31.6805, 0])[0]) < 1e-10, 'Viewport center must use the same conversion as geometry')
assert.ok(oldLayers.every(layer => !layers.has(layer)), 'Switching replaces obsolete geometry instead of leaving duplicate layers')
scene.setView('3d', route); scene.setRoute(route, '3d', [], [])
assert.equal(projections.at(-1), 'ecef'); assert.equal(poses.at(-1).pitch, 52)
assert.equal(JSON.stringify(route), original)
const flatScene = new context.exports.BaiduScene(engine, sdk, { getBoundingClientRect: () => ({ width: 800, height: 600 }) }, true, 'wgs84')
const beforeFlat = new Set(layers)
flatScene.setView('3d', route); flatScene.setRoute(route, '3d', [], [])
assert.equal(projections.at(-1), 'EPSG:3857', 'OSM mode keeps its Web Mercator projection in 3D')
const flatCore = [...layers].find(layer => !beforeFlat.has(layer) && layer.options.lineWidth === 4)
assert.equal(JSON.stringify(coordinates(flatCore)[0]), JSON.stringify(route[0]), 'OSM mode keeps route coordinates in WGS84 without BD-09 conversion')
flatScene.dispose()
const frustumCorners = [[119.95, 31.683, 280], [119.952, 31.683, 280], [119.952, 31.683, 2], [119.95, 31.683, 2]]
const footprintBefore = JSON.stringify(frustumCorners)
scene.setFootprint(route[1], frustumCorners, '3d')
assert.ok([...layers].some(layer => coordinates(layer)?.[0]?.[2] === 180), 'Frustum rays originate at the selected elevated camera')
const faces = [...layers].filter(layer => layer.isMesh)
assert.equal(faces.length, 5, 'A vertical far plane and all four sides must have filled mesh geometry')
for (const face of faces) {
  const positions = face.geometry.attributes.position, indices = face.geometry.index
  for (let i = 0; i < indices.count; i += 3) {
    const a = new Vector3().fromBufferAttribute(positions, indices.getX(i))
    const b = new Vector3().fromBufferAttribute(positions, indices.getX(i + 1))
    const c = new Vector3().fromBufferAttribute(positions, indices.getX(i + 2))
    assert.ok(b.sub(a).cross(c.sub(a)).length() > 1e-9, 'Vertical view faces must not collapse during triangulation')
  }
  assert.equal(face.material.transparent, true)
  assert.equal(face.material.side, DoubleSide)
  assert.equal(face.material.depthWrite, false)
  assert.equal(face.material.depthTest, true)
  assert.equal(face.material.color.getHexString(), '36dfba')
  assert.equal(face.renderOrder, -1, 'Filled faces must render below the yellow and green view lines')
}
const cap = faces[0]
frustumCorners.forEach((corner, index) => {
  const rendered = new Vector3().fromBufferAttribute(cap.geometry.attributes.position, index).add(cap.position)
  const expected = new Vector3().fromArray(toBaiduSceneCoordinate(corner))
  assert.ok(rendered.distanceTo(expected) < 1e-4, 'The green plane must preserve all four geographic heights')
})
const disposed = new Set()
faces.forEach(face => {
  face.geometry.addEventListener('dispose', () => disposed.add(face.geometry))
  face.material.addEventListener('dispose', () => disposed.add(face.material))
})
scene.setFootprint(route[1], frustumCorners, '2d')
assert.equal(disposed.size, 10, 'Switching modes must release every old mesh and material')
assert.ok(faces.every(face => !layers.has(face)))
const flatFill = [...layers].find(layer => layer.dataSource?.features[0].geometry.type === 'Polygon')
assert.ok(coordinates(flatFill)[0].every(corner => corner[2] === 2), '2D footprint stays on the ground')
assert.equal(flatFill.options.transparent, true)
assert.equal(JSON.stringify(frustumCorners), footprintBefore, 'Flattening the display must preserve the mission geometry')
scene.setFootprint(route[0], frustumCorners, '3d')
scene.setFootprint(null, [], '3d')
assert.equal([...layers].filter(layer => layer.isMesh).length, 0, 'Deselection removes the old green view volume')
const pose = { longitude: 119.95, latitude: 31.68, altitude: 100, heading: 45, pitch: -30 }
scene.setPose(pose); const first = poses.at(-1)
scene.setPose({ ...pose, heading: 90, pitch: 15 }); const second = poses.at(-1)
assert.deepEqual(first.p, second.p); assert.equal(second.range, undefined)
assert.deepEqual(first.p, Array.from(toBaiduSceneCoordinate([pose.longitude, pose.latitude, pose.altitude])), 'Preview camera and route must share the BD-09 position')
assert.equal(first.heading, 315); assert.equal(first.pitch, 60)
assert.equal(second.heading, 270); assert.equal(second.pitch, 105)
// followAt must convert the ground-intersection range into a target range:
// feeding getRange() back verbatim grows the distance every frame.
let followPitch = 60, followRange = 900
engine.map.getPitch = () => followPitch
engine.map.getRange = () => followRange + 100 / Math.cos(followPitch * Math.PI / 180)
const followPose = { longitude: 119.95, latitude: 31.68, altitude: 100, heading: 45, pitch: -30 }
for (let i = 0; i < 4; i++) {
  scene.followAt(followPose, '3d')
  followRange = poses.at(-1).range
  assert.ok(Math.abs(followRange - 900) < 1e-6, 'Repeated followAt calls must hold the observer range instead of climbing')
}
assert.equal(poses.at(-1).heading, 315)
followPitch = 0
scene.followAt(followPose, '2d')
assert.equal(poses.at(-1).pitch, 0)
assert.ok(Math.abs(poses.at(-1).range - 900) < 1e-6, '2D follow must strip the altitude leg at pitch 0')
followRange = Number.NaN
scene.followAt(followPose, '3d')
assert.equal(poses.at(-1).range, 900, 'An unreadable range falls back instead of feeding back garbage')
const projectCalls = []
engine.map.projectArrayCoordinate = (p, output) => { projectCalls.push([...p]); output.push(...p); return output }
scene.project(route[0])
assert.deepEqual(projectCalls.at(-1), Array.from(toBaiduSceneCoordinate(route[0])), 'Node hit targets must match the converted route')
const restoredPick = scene.pick(20, 30)
const expectedPick = fromBaiduSceneCoordinate([119.95, 31.68, 1])
assert.ok(Math.abs(restoredPick[0] - expectedPick[0]) < 1e-10)
assert.ok(Math.abs(restoredPick[1] - expectedPick[1]) < 1e-10, 'Map picking must return WGS84 to route planning')
// Isolate clipping math from the geographic datum conversion below.
engine.map.projectArrayCoordinate = (p, output) => { output.push(0, 0, p[2]); return output }
assert.equal(scene.project([0, 0, 0])?.x, 400)
engine.rendering.rawCamera = camera.clone()
const absolutePosition = camera.position.clone()
camera.position.set(10, 20, 30); camera.updateMatrixWorld()
assert.equal(scene.project([0, 0, 0])?.x, 400, 'Node projection must use the absolute camera while the SDK offsets its rendering origin')
camera.position.copy(absolutePosition); camera.updateMatrixWorld()
delete engine.rendering.rawCamera
assert.equal(scene.project([0, 0, camera.position.z + 100]), null, 'Nodes behind the camera cannot be clicked')
engine.map.isGlobe = true
assert.equal(scene.project([0, 0, -6356752]), null, 'Nodes on the far side of Earth must be hidden')
assert.equal(scene.project([0, 0, 6356754])?.x, 400)
scene.setActive(false); assert.equal(engine.map._map.controller.enableInputs, false)
scene.setActive(true); assert.equal(engine.map._map.controller.enableInputs, true)
const previewScene = new context.exports.BaiduScene(engine, sdk, { getBoundingClientRect: () => ({ width: 320, height: 200 }) }, false)
previewScene.setActive(true)
assert.equal(engine.map._map.controller.enableInputs, false, 'Showing the preview must not restore default camera motion')
scene.dispose(); assert.equal(layers.size, 0)
console.log('Passed: elevated render geometry, north-up 2D, globe projection, fixed preview pose, stable follow range, camera clipping and Earth occlusion.')

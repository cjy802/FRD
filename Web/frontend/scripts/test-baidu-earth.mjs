import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import { parse } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as Vue from 'vue'

const source = await readFile(new URL('../src/EarthMap.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const props = Vue.reactive({ active: true, provider: 'baidu', viewMode: '3d', start: { longitude: 119.95, latitude: 31.68 },
  end: { longitude: 119.951, latitude: 31.681 }, route: [[119.95, 31.68, 100], [119.951, 31.681, 120]],
  selectedIndex: null, editMode: 'node', footprint: [], weatherSegments: [], showStartRange: false, showEndRange: false, rangeMeters: 35 })
const calls = { view: [], routes: [], focus: [], footprints: [], events: [] }
const captures = new Set(), listeners = new Map()
let mounted, cleanup, frame
const element = { getBoundingClientRect: () => ({ left: 10, top: 20, width: 800, height: 600 }),
  setPointerCapture: id => captures.add(id), hasPointerCapture: id => captures.has(id), releasePointerCapture: id => captures.delete(id) }
const scene = { engine: { controller: { enabled: true } },
  setInputs: enabled => { scene.engine.controller.enabled = enabled },
  setView: (...args) => calls.view.push(args), fit() {}, setActive() {}, resize() {}, dispose() {},
  setRoute: (...args) => calls.routes.push(args), setFootprint: (...args) => calls.footprints.push(args),
  focus: (...args) => calls.focus.push(args), setTilt() {}, onFrame: callback => { frame = callback },
  project: ([lng, lat, altitude]) => ({ x: (lng - 119.95) * 10000 + 100, y: (lat - 31.68) * 10000 + 100 - altitude }),
  pick: (x, y) => [119.95 + x / 10000, 31.68 + y / 10000, 0],
}
const context = vm.createContext({ exports: {},
  require: name => name === 'vue' ? { ...Vue, onMounted: cb => { mounted = cb }, onBeforeUnmount: cb => { cleanup = cb } }
    : { createMapScene: async () => scene },
  defineProps: () => props, defineEmits: () => (...args) => calls.events.push(args), defineExpose() {},
  ResizeObserver: class { observe() {} disconnect() {} },
  performance: { now: () => 0 },
  window: { addEventListener: (name, cb) => listeners.set(name, cb), removeEventListener: name => listeners.delete(name) },
})
const compiled = ts.transpileModule(`${descriptor.scriptSetup.content}\nglobalThis.earth = {
  overlay, initialize, onMapPointerDown, onMapPointerMove, onMapClick, onNodeDown, onNodeMove, onNodeUp,
  focusWaypoint, finishDrag, setElement() { earthElement.value = globalThis.element },
}`, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText
context.element = element
new vm.Script(compiled).runInContext(context)
const earth = context.earth
earth.setElement(); await earth.initialize()
frame()
assert.equal(calls.view.at(-1)[0], '3d')
assert.equal(calls.routes.at(-1)[0][1][2], 120, 'Preserve per-node altitude in the globe renderer')
assert.equal(earth.overlay.nodes.length, 2)
assert.equal(earth.overlay.groundNodes.length, 2, '3D edit mode exposes a ground projection for every elevated node')
const route = JSON.stringify(props.route)
props.viewMode = '2d'; await Vue.nextTick(); frame()
assert.equal(calls.view.at(-1)[0], '2d', 'The reactive mode switch must reach the renderer')
assert.equal(earth.overlay.nodes[0].y, 98, '2D node hit targets follow the ground projection')
assert.equal(earth.overlay.groundNodes.length, 0, '2D mode does not duplicate ground projections')
assert.equal(JSON.stringify(props.route), route, 'Flattening the display must preserve mission heights')
const pointer = extra => ({ button: 0, isPrimary: true, pointerId: 1, clientX: 110, clientY: 120,
  currentTarget: element, preventDefault() {}, stopPropagation() {}, ...extra })
earth.onNodeDown(pointer(), 1)
assert.equal(calls.events.at(-1)[0], 'select-waypoint')
assert.equal(calls.events.at(-1)[1], 1)
assert.equal(scene.engine.controller.enabled, false)
earth.onNodeMove(pointer({ clientX: 112 }))
assert.equal(calls.events.at(-1)[0], 'select-waypoint', 'Small pointer jitter must not rotate a clicked node')
earth.onNodeMove(pointer({ clientX: 170 }))
assert.equal(calls.events.at(-1)[0], 'rotate-waypoint')
assert.ok(Number.isFinite(calls.events.at(-1)[1]))
assert.equal(JSON.stringify(props.route), route, 'Rotation must preserve node position and altitude')
earth.onNodeUp(pointer())
assert.equal(captures.size, 0)
assert.equal(scene.engine.controller.enabled, true)
earth.focusWaypoint(1)
assert.equal(calls.focus.at(-1)[1], '2d')
props.selectedIndex = 1; props.footprint = [[119.951, 31.681], [119.952, 31.681], [119.952, 31.682]]
assert.equal(calls.footprints.at(-1)[0][2], 120, 'The frustum origin retains the camera height')
props.editMode = 'route'; await Vue.nextTick()
earth.onMapPointerDown(pointer()); earth.onMapPointerMove(pointer({ clientX: 180 })); earth.onMapClick(pointer())
assert.equal(calls.events.at(-1)[0], 'rotate-waypoint', 'Panning must not change A/B')
earth.onMapPointerDown(pointer()); earth.onMapClick(pointer())
assert.equal(calls.events.at(-1)[0], 'choose-point')
assert.ok(Math.abs(calls.events.at(-1)[1].longitude - 119.96) < 1e-8, 'The renderer returns WGS84 without a second coordinate offset')
props.active = false; await Vue.nextTick()
const count = calls.events.length
earth.onMapClick(pointer()); earth.onNodeDown(pointer(), 0)
assert.equal(calls.events.length, count, 'A hidden map cannot edit nodes or A/B')
props.active = true; props.viewMode = '3d'; await Vue.nextTick()
assert.equal(calls.view.at(-1)[0], '3d')
assert.equal(JSON.stringify(props.route), route)
cleanup()
assert.equal(listeners.size, 0)
console.log('Passed: reactive Earth 2D/3D switching, route heights, node selection/drag threshold/cancellation, frustum origin and WGS84 clicks.')

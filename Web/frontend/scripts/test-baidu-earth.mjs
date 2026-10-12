import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import { parse } from '@vue/compiler-sfc'
import ts from 'typescript'

function loadTypeScript(source) {
  const context = vm.createContext({ exports: {}, Math })
  new vm.Script(ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText).runInContext(context)
  return context.exports
}

const coordinates = loadTypeScript(await readFile(new URL('../src/baiduCoordinates.ts', import.meta.url), 'utf8'))
for (const point of [[119.9515, 31.6792], [116.404, 39.915], [114.0579, 22.5431]]) {
  const converted = coordinates.wgs84ToBd09(point)
  const restored = coordinates.bd09ToWgs84(converted)
  assert.ok(Math.abs(restored[0] - point[0]) < 0.00001)
  assert.ok(Math.abs(restored[1] - point[1]) < 0.00001)
  assert.ok(Math.abs(converted[0] - point[0]) > 0.005, 'Chinese points require the Baidu coordinate offset')
}

const component = await readFile(new URL('../src/EarthMap.vue', import.meta.url), 'utf8')
const { descriptor } = parse(component)
const mounted = []
const unmounted = []
const frames = new Map()
const events = []
const calls = { type: null, center: null, zoom: null, resize: 0, destroyed: false }
const windowListeners = new Map()
const timers = new Map()
const scripts = []
let frameId = 0
const props = {
  active: true,
  start: { longitude: 119.9515, latitude: 31.6792 },
  end: { longitude: 119.9495, latitude: 31.6804 },
  route: [[119.9515, 31.6792], [119.9505, 31.6798], [119.9495, 31.6804]],
  selectedIndex: 1,
  editMode: 'node',
  footprint: [[119.9504, 31.6799], [119.9507, 31.6799], [119.9507, 31.6801], [119.9504, 31.6801]],
  weatherSegments: [],
  showStartRange: false,
  showEndRange: false,
  rangeMeters: 35,
}
const origin = coordinates.wgs84ToBd09([119.9505, 31.6798])
const BMapGL = {
  Point: class { constructor(lng, lat) { this.lng = lng; this.lat = lat } },
  Pixel: class { constructor(x, y) { this.x = x; this.y = y } },
  Map: class {
    constructor() { this.listeners = new Map(); calls.map = this }
    centerAndZoom(center, zoom) { calls.center = center; calls.zoom = zoom }
    setMapType(type) { calls.type = type }
    enableScrollWheelZoom() {}
    addEventListener(name, callback) { this.listeners.set(name, callback) }
    pointToPixel(point) { return { x: 400 + (point.lng - origin[0]) * 40000, y: 300 - (point.lat - origin[1]) * 40000 } }
    pixelToPoint(pixel) { return { lng: origin[0] + (pixel.x - 400) / 40000, lat: origin[1] - (pixel.y - 300) / 40000 } }
    resize() { calls.resize++ }
    destroy() { calls.destroyed = true }
  },
}
const vue = {
  ref: value => ({ value }),
  reactive: value => value,
  nextTick: () => Promise.resolve(),
  watch: () => {},
  onMounted: callback => mounted.push(callback),
  onBeforeUnmount: callback => unmounted.push(callback),
}
const element = { getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 600 }) }
const context = vm.createContext({
  exports: {},
  require: name => name === 'vue' ? vue : name === './baiduCoordinates' ? coordinates : {},
  defineProps: () => props,
  defineEmits: () => (...args) => events.push(args),
  window: {
    BMAP_EARTH_MAP: 'earth',
    addEventListener: (name, callback) => windowListeners.set(name, callback),
    removeEventListener: name => windowListeners.delete(name),
    setTimeout: (callback, delay) => { timers.set(delay, callback); return delay },
    clearTimeout: id => timers.delete(id),
  },
  document: {
    createElement: () => ({ remove() {} }),
    head: { appendChild(script) {
      scripts.push(script)
      const callback = new URL(script.src).searchParams.get('callback')
      assert.ok(callback, 'Dynamic loading must use the official callback parameter')
      context.window.BMapGL = BMapGL
      context.window[callback]()
    } },
  },
  ResizeObserver: class { observe() {} disconnect() {} },
  requestAnimationFrame: callback => { const id = ++frameId; frames.set(id, callback); return id },
  cancelAnimationFrame: id => frames.delete(id),
  Math, Number,
})
const source = descriptor.scriptSetup.content.replace('import.meta.env.VITE_BAIDU_MAP_AK', "'test-key'")
const compiled = ts.transpileModule(`${source}\nglobalThis.earth = {
  ready, overlay, onMapPointerDown, onMapPointerMove, onMapPointerUp, onMapClick, onMapDomClick,
  onNodeDown, onNodeMove, onNodeUp, scheduleOverlay,
  setElement() { earthElement.value = globalThis.mockElement },
}`, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText
context.mockElement = element
new vm.Script(compiled, { filename: 'EarthMap.vue' }).runInContext(context)
const earth = context.earth
earth.setElement()
await mounted[0]()
assert.equal(scripts.length, 1)
assert.equal(timers.size, 0, 'Successful initialization must clear the timeout')
assert.equal(calls.type, 'earth', 'The Baidu JSAPI map must use BMAP_EARTH_MAP')
assert.equal(calls.zoom, 17)
assert.equal(earth.ready.value, true)
const render = () => {
  const callbacks = [...frames.values()]
  frames.clear()
  callbacks.forEach(callback => callback())
}
render()
assert.ok(earth.overlay.route.startsWith('M'))
assert.ok(earth.overlay.footprint.endsWith(' Z'))
assert.equal(earth.overlay.nodes.length, props.route.length)

earth.onMapPointerDown({ clientX: 400, clientY: 300 })
earth.onMapPointerMove({ clientX: 420, clientY: 300 })
earth.onMapPointerUp()
props.editMode = 'route'
const click = calls.map.listeners.get('click')
assert.equal(typeof click, 'function', 'Register the GL map click handler')
click({ latlng: { lng: origin[0], lat: origin[1] } })
earth.onMapDomClick({ clientX: 400, clientY: 300 })
render()
assert.equal(events.length, 0, 'Panning the globe must not set an A/B point')
earth.onMapPointerDown({ clientX: 400, clientY: 300 })
earth.onMapPointerUp()
click({ latlng: { lng: origin[0], lat: origin[1] } })
earth.onMapDomClick({ clientX: 410, clientY: 310 })
render()
assert.equal(events.length, 1, 'SDK and DOM events must set a point only once')
assert.equal(events[0][0], 'choose-point')
assert.ok(Math.abs(events[0][1].longitude - 119.9505) < 0.00002)
assert.ok(Math.abs(events[0][1].latitude - 31.6798) < 0.00002)

const endpointCount = events.length
click({ point: { lng: origin[0], lat: origin[1] } })
render()
click({ pixel: { x: 400, y: 300 } })
render()
earth.onMapDomClick({ clientX: 400, clientY: 300 })
render()
assert.equal(events.length, endpointCount + 3, 'Support legacy point events, pixel-only events and DOM click fallback')
for (const event of [{}, { latlng: { lng: NaN, lat: 31 } }, { latlng: { lng: 119, lat: 91 } }]) click(event)
render()
assert.equal(events.length, endpointCount + 3, 'Invalid clicks must not reach the route planner')
props.active = false
click({ latlng: { lng: origin[0], lat: origin[1] } })
earth.onMapDomClick({ clientX: 400, clientY: 300 })
render()
assert.equal(events.length, endpointCount + 3, 'A hidden Earth map must not change endpoints')
props.active = true

props.editMode = 'node'
const nodeTarget = { setPointerCapture() {}, hasPointerCapture: () => true, releasePointerCapture() {} }
earth.onNodeDown({ stopPropagation() {}, pointerId: 1, currentTarget: nodeTarget }, 1)
earth.onNodeMove({ pointerId: 1, clientX: 450, clientY: 300 })
earth.onNodeUp({ pointerId: 1, currentTarget: nodeTarget })
assert.equal(events.at(-2)[0], 'select-waypoint')
assert.equal(events.at(-1)[0], 'rotate-waypoint')
assert.ok(Number.isFinite(events.at(-1)[1]))

unmounted[0]()
assert.equal(calls.destroyed, true)
assert.equal(windowListeners.size, 0)
console.log('Passed: WGS84/BD09 conversion, Earth map type, projected overlays, map point selection, drag handling, and cleanup.')

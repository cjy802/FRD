import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import { parse } from '@vue/compiler-sfc'
import ts from 'typescript'

async function loadComponent(name, hooks, globals = {}) {
  const source = await readFile(new URL(`../src/${name}.vue`, import.meta.url), 'utf8')
  const { descriptor } = parse(source)
  const events = []
  const cleanup = []
  const timers = new Map()
  let now = Date.parse('2026-10-12T00:00:00Z')
  let timerId = 0
  const vue = {
    ref: value => ({ value }),
    reactive: value => value,
    computed: getter => ({ get value() { return getter() } }),
    watch: () => {},
    onMounted: () => {},
    onBeforeUnmount: callback => cleanup.push(callback),
    nextTick: callback => Promise.resolve().then(() => callback?.()),
  }
  const context = vm.createContext({
    exports: {},
    require: name => name === 'vue' ? vue : {},
    defineProps: () => ({ start: { latitude: 31.6792, longitude: 119.9515 }, end: { latitude: 31.6804, longitude: 119.9495 } }),
    defineEmits: () => (...args) => events.push(args),
    AbortController, URLSearchParams, console,
    Date: class extends Date { static now() { return now } },
    setTimeout: (callback, delay) => {
      const id = ++timerId
      timers.set(id, { callback, delay })
      return id
    },
    clearTimeout: id => timers.delete(id),
    setInterval: () => ++timerId,
    clearInterval: () => {},
    ...globals,
  })
  const compiled = ts.transpileModule(`${descriptor.scriptSetup.content}\nglobalThis.hooks = ${hooks}`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText
  new vm.Script(compiled, { filename: `${name}.vue` }).runInContext(context)
  return {
    ...context.hooks, events, cleanup, timers, context,
    setTime: value => { now = value },
    tick: async () => {
      const [id, timer] = timers.entries().next().value
      timers.delete(id)
      now += timer.delay
      timer.callback()
      for (let i = 0; i < 10; i++) await Promise.resolve()
    },
  }
}

const pendingJob = { job_id: 'job-1', status: 'running', message: 'Preparing weather', forecast: null }
const response = payload => ({ ok: true, json: async () => payload })
const requests = []
let job = pendingJob
const weather = await loadComponent('WeatherPanels', '{ predictWeather, loading, completedJob, error }', {
  fetch: async url => { requests.push(url); return response(job) },
})
await weather.predictWeather()
const initialEvents = weather.events.length
assert.equal(weather.timers.values().next().value.delay, 2000)
for (let i = 0; i < 5; i++) await weather.tick()
assert.equal(weather.timers.values().next().value.delay, 5000)
for (let i = 0; i < 10; i++) await weather.tick()
assert.equal(weather.timers.values().next().value.delay, 10_000)
assert.equal(weather.events.length, initialEvents, 'Identical progress must not refresh the parent')
assert.equal(requests.filter(url => url === '/api/weather/predict').length, 1)
job = { ...pendingJob, status: 'failed', message: 'Worker stopped' }
await weather.tick()
assert.equal(weather.loading.value, false)
assert.equal(weather.timers.size, 0, 'A terminal job must stop polling')

for (const status of ['succeeded', 'blocked']) {
  const terminal = await loadComponent('WeatherPanels', '{ predictWeather, loading, completedJob }', {
    fetch: async url => response(url.endsWith('/predict') ? {
      ...pendingJob, status,
      forecast: status === 'succeeded' ? { historical: false, valid_time_utc: '2099-01-01T00:00:00Z' } : null,
    } : {}),
  })
  await terminal.predictWeather()
  assert.equal(terminal.loading.value, false)
  assert.equal(terminal.timers.size, 0)
  if (status === 'succeeded') assert.equal(terminal.completedJob.value, 'job-1')
}

const timeout = await loadComponent('WeatherPanels', '{ predictWeather, loading, error }', {
  fetch: async () => response(pendingJob),
})
await timeout.predictWeather()
timeout.setTime(Date.parse('2026-10-12T00:30:00Z'))
await timeout.tick()
assert.equal(timeout.loading.value, false)
assert.ok(timeout.error.value)
assert.equal(timeout.timers.size, 0, 'The elapsed-time limit must stop polling')

const deferred = []
const stale = await loadComponent('WeatherPanels', '{ predictWeather, loading }', {
  fetch: () => new Promise(resolve => deferred.push(resolve)),
})
const first = stale.predictWeather()
const second = stale.predictWeather()
deferred[0](response(pendingJob))
await first
assert.equal(stale.timers.size, 0, 'A replaced request must not schedule another poll')
deferred[1](response(pendingJob))
await second
stale.cleanup.forEach(callback => callback())
assert.equal(stale.timers.size, 0, 'Unmount must cancel pending polling')

let routeDraws = 0
let viewUpdates = 0
let previewUpdates = 0
const frames = new Map()
let frameId = 0
const app = await loadComponent('App', `{
  adjustSelectedWaypoint, moveSelectedWaypoint, waypointSettings, preview, selectedWaypointIndex,
  updateSelectedWaypointView,
  getSelectedViewFrustum() { return selectedViewFrustum },
  setViewEnvironment(entities, library) {
    viewer = { entities, scene: { requestRender() {} } }
    Cesium = library
    mapEditMode.value = 'node'
  },
  instrument() {
    drawCesiumRoute = () => { routeDraws++ }
    updateSelectedWaypointView = () => { viewUpdates++ }
    updateCameraPreview = () => { previewUpdates++ }
  }
}`, {
  routeDraws, viewUpdates, previewUpdates,
  requestAnimationFrame: callback => { const id = ++frameId; frames.set(id, callback); return id },
  cancelAnimationFrame: id => frames.delete(id),
  window: { removeEventListener() {} },
})
app.instrument()
app.selectedWaypointIndex.value = 0
app.waypointSettings.value = [{ altitude_m: 100, heading_mode: 'fixed', heading_angle: 0, gimbal_pitch_angle: -30 }]
app.preview.value = { geometry: { coordinates: [[119.95, 31.68]] } }
const render = () => {
  const callbacks = [...frames.values()]
  frames.clear()
  callbacks.forEach(callback => callback())
}
for (let angle = 1; angle <= 30; angle++) app.adjustSelectedWaypoint({ heading_angle: angle })
assert.equal(frames.size, 1, 'Mouse events within one frame must be coalesced')
render()
assert.equal(app.context.routeDraws, 0, 'Heading changes must preserve the route entities')
assert.equal(app.context.viewUpdates, 1)
assert.equal(app.context.previewUpdates, 1)
app.adjustSelectedWaypoint({ altitude_m: 110 })
app.adjustSelectedWaypoint({ heading_angle: 40 })
render()
assert.equal(app.context.routeDraws, 1, 'Altitude changes must still update the full route')
app.moveSelectedWaypoint(1, 0)
render()
assert.equal(app.context.routeDraws, 2, 'Waypoint movement must still update the full route')
assert.notEqual(app.preview.value.geometry.coordinates[0][0], 119.95)
app.adjustSelectedWaypoint({ heading_angle: 50 })
assert.equal(frames.size, 1)
app.cleanup.forEach(callback => callback())
assert.equal(frames.size, 0, 'Unmount must cancel a pending render')

const entities = new Map([
  ['waypoint-direction-0', { billboard: {} }],
  ['selected-view-direction', { polyline: {} }],
  ...Array.from({ length: 4 }, (_, index) => [`selected-view-face-${index}`, { polygon: {} }]),
  ['selected-view-rays', { polyline: {} }],
  ['selected-view-border', { polyline: {} }],
])
app.setViewEnvironment({ getById: id => entities.get(id) }, {
  Cartesian3: { fromDegrees: (longitude, latitude, altitude) => ({ longitude, latitude, altitude }) },
  Math: { toRadians: angle => angle * Math.PI / 180 },
  PolygonHierarchy: class { constructor(positions) { this.positions = positions } },
})
app.updateSelectedWaypointView()
assert.equal(entities.get('waypoint-direction-0').billboard.rotation, (50 - 90) * Math.PI / 180)
assert.equal(app.getSelectedViewFrustum().origin.altitude, 110)
for (let index = 0; index < 4; index++) {
  assert.equal(app.getSelectedViewFrustum().faces[index].length, 3)
}
assert.equal(app.getSelectedViewFrustum().ground.length, 4)

console.log('Passed: polling backoff, unchanged status, terminal jobs, timeout, cancellation, and batched waypoint updates.')

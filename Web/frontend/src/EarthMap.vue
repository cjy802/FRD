<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { bd09ToWgs84, wgs84ToBd09, type LngLat } from './baiduCoordinates'

type Point = { longitude: number; latitude: number }
type WeatherSegment = { start: number[]; end: number[]; level: string }
type ScreenPoint = { x: number; y: number }

const props = defineProps<{
  active: boolean
  viewMode: '2d' | '3d'
  start: Point
  end: Point
  route: number[][] | null
  selectedIndex: number | null
  editMode: 'route' | 'node'
  footprint: number[][]
  weatherSegments: WeatherSegment[]
  showStartRange: boolean
  showEndRange: boolean
  rangeMeters: number
}>()
const emit = defineEmits<{
  'select-waypoint': [index: number]
  'choose-point': [point: Point]
  'rotate-waypoint': [heading: number]
}>()

const earthElement = ref<HTMLElement | null>(null)
const status = ref('正在加载地球模式…')
const ready = ref(false)
const failed = ref(false)
const overlay = reactive({
  width: 1,
  height: 1,
  route: [] as string[],
  footprint: '',
  rays: '',
  startRange: '',
  endRange: '',
  weather: [] as { path: string; color: string }[],
  start: null as ScreenPoint | null,
  end: null as ScreenPoint | null,
  nodes: [] as (ScreenPoint & { index: number })[],
})

let map: any = null
let BMapGL: any = null
let renderFrame: number | null = null
let resizeObserver: ResizeObserver | null = null
let dragIndex: number | null = null
let dragPointer: number | null = null
let keepRendering = false
let mapPointerStart: ScreenPoint | null = null
let mapPointerMoved = false
let pointClickFrame: number | null = null
let pendingPointClick: { lng: number; lat: number; priority: number } | null = null

let scriptPromise: Promise<any> | null = null
function loadBaiduMap() {
  if ((window as any).BMapGL?.Map) return Promise.resolve((window as any).BMapGL)
  if (scriptPromise) return scriptPromise
  const ak = import.meta.env.VITE_BAIDU_MAP_AK?.trim()
  if (!ak) return Promise.reject(new Error('尚未配置百度地图 AK。'))
  scriptPromise = new Promise((resolve, reject) => {
    const script = document.createElement('script')
    const callbackName = `__baiduEarthReady${Date.now()}`
    let settled = false
    ;(window as any)[callbackName] = () => {
      if (settled) return
      settled = true
      window.clearTimeout(deadline)
      delete (window as any)[callbackName]
      if ((window as any).BMapGL?.Map) resolve((window as any).BMapGL)
      else reject(new Error('百度地图初始化失败，请检查 AK 配置。'))
    }
    const deadline = window.setTimeout(() => {
      settled = true
      script.remove()
      delete (window as any)[callbackName]
      reject(new Error('百度地图加载超时。'))
    }, 15000)
    script.src = `https://api.map.baidu.com/api?v=1.0&type=webgl&ak=${encodeURIComponent(ak)}&callback=${callbackName}`
    script.onerror = () => {
      settled = true
      window.clearTimeout(deadline)
      script.remove()
      delete (window as any)[callbackName]
      reject(new Error('百度地图脚本加载失败。'))
    }
    document.head.appendChild(script)
  }).catch((error) => { scriptPromise = null; throw error })
  return scriptPromise
}

function toPoint([longitude, latitude]: number[]): any {
  const [lng, lat] = wgs84ToBd09([longitude, latitude])
  return new BMapGL.Point(lng, lat)
}

function project(coordinate: number[]): ScreenPoint | null {
  if (!map) return null
  const pixel = map.pointToPixel(toPoint(coordinate))
  if (!pixel || !Number.isFinite(pixel.x) || !Number.isFinite(pixel.y)) return null
  return { x: pixel.x, y: pixel.y }
}

function path(coordinates: number[][], close = false) {
  const pixels = coordinates.map(project)
  if (pixels.some(pixel => !pixel)) return ''
  const points = pixels as ScreenPoint[]
  return points.map((point, index) => `${index ? 'L' : 'M'}${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(' ') + (close ? ' Z' : '')
}

function paths(coordinates: number[][]) {
  const result: string[] = []
  let segment: number[][] = []
  const flush = () => {
    if (segment.length > 1) result.push(path(segment))
    segment = []
  }
  coordinates.forEach((coordinate) => {
    if (project(coordinate)) segment.push(coordinate)
    else flush()
  })
  flush()
  return result
}

function circle(point: Point) {
  const latitudeRadians = point.latitude * Math.PI / 180
  const latitudeStep = props.rangeMeters / 111320
  const longitudeStep = latitudeStep / Math.cos(latitudeRadians)
  return path(Array.from({ length: 32 }, (_, index) => {
    const angle = index * Math.PI / 16
    return [point.longitude + longitudeStep * Math.cos(angle), point.latitude + latitudeStep * Math.sin(angle)]
  }), true)
}

function renderOverlay() {
  if (!map || !earthElement.value || !props.active) return
  const bounds = earthElement.value.getBoundingClientRect()
  overlay.width = Math.max(1, bounds.width)
  overlay.height = Math.max(1, bounds.height)
  overlay.route = props.route?.length && props.route.length > 1 ? paths(props.route) : []
  overlay.start = project([props.start.longitude, props.start.latitude])
  overlay.end = project([props.end.longitude, props.end.latitude])
  overlay.startRange = props.showStartRange ? circle(props.start) : ''
  overlay.endRange = props.showEndRange ? circle(props.end) : ''
  overlay.nodes = props.editMode === 'node' ? (props.route ?? []).flatMap((point, index) => {
    const pixel = project(point)
    return pixel ? [{ ...pixel, index }] : []
  }) : []
  overlay.footprint = props.selectedIndex !== null && props.footprint.length >= 3 ? path(props.footprint, true) : ''
  overlay.rays = props.selectedIndex !== null && props.route?.[props.selectedIndex]
    ? props.footprint.map(point => path([props.route![props.selectedIndex!], point])).join(' ')
    : ''
  const colors: Record<string, string> = { no_trigger: '#0f6cbd', attention: '#b66a00', elevated: '#b42318', incomplete: '#6e7781' }
  overlay.weather = props.weatherSegments.map(segment => ({
    path: path([segment.start, segment.end]), color: colors[segment.level] ?? '#6e7781',
  }))
}

function scheduleOverlay() {
  if (renderFrame !== null) return
  renderFrame = requestAnimationFrame(() => {
    renderFrame = null
    renderOverlay()
    if (keepRendering && props.active) scheduleOverlay()
  })
}

function fitRoute() {
  if (!map || !BMapGL) return
  const coordinates = props.route?.length ? props.route : [[props.start.longitude, props.start.latitude], [props.end.longitude, props.end.latitude]]
  const points = coordinates.map(toPoint)
  if (points.length > 1) map.setViewport?.(points, { margins: [48, 48, 48, 48], zoomFactor: -1 })
}

function applyViewMode() {
  if (!map || !BMapGL) return
  const mapType = props.viewMode === '3d' ? (window as any).BMAP_EARTH_MAP : (window as any).BMAP_SATELLITE_MAP
  if (mapType) map.setMapType(mapType)
  fitRoute()
  scheduleOverlay()
}

function onMapPointerDown(event: PointerEvent) {
  mapPointerStart = { x: event.clientX, y: event.clientY }
  mapPointerMoved = false
  keepRendering = true
  scheduleOverlay()
}
function onMapPointerMove(event: PointerEvent) {
  if (mapPointerStart && Math.hypot(event.clientX - mapPointerStart.x, event.clientY - mapPointerStart.y) > 5) mapPointerMoved = true
}
function onMapPointerUp() { keepRendering = false; mapPointerStart = null; scheduleOverlay() }
function stopOverlayTracking() { keepRendering = false; mapPointerStart = null }

function onMapClick(event: {
  latlng?: { lng: number; lat: number }
  point?: { lng: number; lat: number }
  pixel?: { x: number; y: number }
}) {
  if (mapPointerMoved || !props.active || props.editMode !== 'route') return
  // JSAPI GL uses latlng; retain point/pixel fallbacks for older event variants.
  const point = event.latlng ?? event.point ?? (
    event.pixel && map && BMapGL && Number.isFinite(event.pixel.x) && Number.isFinite(event.pixel.y)
      ? map.pixelToPoint(new BMapGL.Pixel(event.pixel.x, event.pixel.y))
      : null
  )
  if (!point || !Number.isFinite(point.lng) || !Number.isFinite(point.lat)
    || Math.abs(point.lng) > 180 || Math.abs(point.lat) > 90) return
  const priority = event.latlng ? 2 : event.point ? 1 : 0
  if (!pendingPointClick || priority >= pendingPointClick.priority) pendingPointClick = { lng: point.lng, lat: point.lat, priority }
  if (pointClickFrame !== null) return
  // The SDK event and DOM fallback can describe the same click. Emit once,
  // using the SDK's geographic coordinate when both are available.
  pointClickFrame = requestAnimationFrame(() => {
    pointClickFrame = null
    const selected = pendingPointClick
    pendingPointClick = null
    if (!selected || mapPointerMoved || !props.active || props.editMode !== 'route') return
    const [longitude, latitude] = bd09ToWgs84([selected.lng, selected.lat])
    emit('choose-point', { longitude: Number(longitude.toFixed(5)), latitude: Number(latitude.toFixed(5)) })
  })
}

function onMapDomClick(event: MouseEvent) {
  if (!map || !BMapGL || !earthElement.value) return
  const bounds = earthElement.value.getBoundingClientRect()
  const x = event.clientX - bounds.left
  const y = event.clientY - bounds.top
  if (x < 0 || y < 0 || x > bounds.width || y > bounds.height) return
  onMapClick({ pixel: { x, y } })
}

function bearing(origin: number[], target: LngLat) {
  const lat1 = origin[1] * Math.PI / 180
  const lat2 = target[1] * Math.PI / 180
  const deltaLongitude = (target[0] - origin[0]) * Math.PI / 180
  return Math.atan2(Math.sin(deltaLongitude) * Math.cos(lat2), Math.cos(lat1) * Math.sin(lat2) - Math.sin(lat1) * Math.cos(lat2) * Math.cos(deltaLongitude)) * 180 / Math.PI
}

function onNodeDown(event: PointerEvent, index: number) {
  event.stopPropagation()
  emit('select-waypoint', index)
  dragIndex = index
  dragPointer = event.pointerId
  ;(event.currentTarget as Element).setPointerCapture(event.pointerId)
}

function onNodeMove(event: PointerEvent) {
  if (dragIndex === null || dragPointer !== event.pointerId || !map || !BMapGL || !props.route?.[dragIndex] || !earthElement.value) return
  const bounds = earthElement.value.getBoundingClientRect()
  const point = map.pixelToPoint(new BMapGL.Pixel(event.clientX - bounds.left, event.clientY - bounds.top))
  if (!point) return
  emit('rotate-waypoint', bearing(props.route[dragIndex], bd09ToWgs84([point.lng, point.lat])))
}

function onNodeUp(event: PointerEvent) {
  if (dragPointer !== event.pointerId) return
  dragIndex = null
  dragPointer = null
  const target = event.currentTarget as Element
  if (target.hasPointerCapture(event.pointerId)) target.releasePointerCapture(event.pointerId)
}

async function initializeEarthMap() {
  failed.value = false
  status.value = '正在加载地球模式…'
  try {
    BMapGL = await loadBaiduMap()
    if (!earthElement.value) return
    map = new BMapGL.Map(earthElement.value, { enableIconClick: false })
    const center = toPoint([(props.start.longitude + props.end.longitude) / 2, (props.start.latitude + props.end.latitude) / 2])
    map.centerAndZoom(center, 17)
    const mapType = props.viewMode === '3d' ? (window as any).BMAP_EARTH_MAP : (window as any).BMAP_SATELLITE_MAP
    if (!mapType) throw new Error('当前百度地图脚本不支持所选地图模式。')
    map.setMapType(mapType)
    fitRoute()
    map.enableScrollWheelZoom()
    for (const eventName of ['moving', 'moveend', 'zooming', 'zoomend', 'heading_changed', 'tilt_changed', 'tilesloaded']) {
      map.addEventListener(eventName, scheduleOverlay)
    }
    map.addEventListener('click', onMapClick)
    resizeObserver = new ResizeObserver(scheduleOverlay)
    resizeObserver.observe(earthElement.value)
    ready.value = true
    status.value = ''
    await nextTick()
    map.resize?.()
    scheduleOverlay()
  } catch (error) {
    ready.value = false
    failed.value = true
    status.value = error instanceof Error ? error.message : '地球模式加载失败。'
    resizeObserver?.disconnect()
    resizeObserver = null
    map?.destroy?.()
    map = null
  }
}

onMounted(() => {
  window.addEventListener('pointerup', stopOverlayTracking)
  window.addEventListener('pointercancel', stopOverlayTracking)
  return initializeEarthMap()
})

watch(() => props.active, async (active) => {
  if (!active) { keepRendering = false; return }
  await nextTick()
  map?.resize?.()
  scheduleOverlay()
})
watch(() => props.viewMode, applyViewMode)
watch(() => props.route, () => { fitRoute(); scheduleOverlay() }, { deep: true })
watch(() => [props.start.longitude, props.start.latitude, props.end.longitude, props.end.latitude], () => {
  if (!map) return
  map.setCenter(toPoint([(props.start.longitude + props.end.longitude) / 2, (props.start.latitude + props.end.latitude) / 2]))
  scheduleOverlay()
})
watch(props, scheduleOverlay, { deep: true })

onBeforeUnmount(() => {
  window.removeEventListener('pointerup', stopOverlayTracking)
  window.removeEventListener('pointercancel', stopOverlayTracking)
  keepRendering = false
  if (renderFrame !== null) cancelAnimationFrame(renderFrame)
  if (pointClickFrame !== null) cancelAnimationFrame(pointClickFrame)
  pendingPointClick = null
  resizeObserver?.disconnect()
  map?.destroy?.()
  map = null
})
</script>

<template>
  <div class="earth-map" :class="{ active }" aria-label="百度地图地球模式">
    <div ref="earthElement" class="earth-map-canvas" @pointerdown="onMapPointerDown" @pointermove="onMapPointerMove" @pointerup="onMapPointerUp" @pointercancel="onMapPointerUp" @click="onMapDomClick"></div>
    <svg v-if="ready" class="earth-map-overlay" :viewBox="`0 0 ${overlay.width} ${overlay.height}`" preserveAspectRatio="none" aria-hidden="true">
      <path v-if="overlay.startRange" :d="overlay.startRange" class="earth-range earth-range-start" />
      <path v-if="overlay.endRange" :d="overlay.endRange" class="earth-range earth-range-end" />
      <path v-for="(routePath, index) in overlay.route" :key="`route-${index}`" :d="routePath" class="earth-route-shadow" />
      <path v-for="(routePath, index) in overlay.route" :key="`route-core-${index}`" :d="routePath" class="earth-route" />
      <path v-for="(segment, index) in overlay.weather" :key="index" :d="segment.path" :stroke="segment.color" class="earth-weather" />
      <path v-if="overlay.footprint" :d="overlay.footprint" class="earth-footprint" />
      <path v-if="overlay.rays" :d="overlay.rays" class="earth-rays" />
      <g v-if="overlay.start" :transform="`translate(${overlay.start.x} ${overlay.start.y})`" class="earth-endpoint"><circle r="9" fill="#0f6cbd" /><text y="-16">A</text></g>
      <g v-if="overlay.end" :transform="`translate(${overlay.end.x} ${overlay.end.y})`" class="earth-endpoint"><circle r="9" fill="#5b5fc7" /><text y="-16">B</text></g>
      <circle v-for="node in overlay.nodes" :key="node.index" :cx="node.x" :cy="node.y" :r="node.index === selectedIndex ? 9 : 6" class="earth-node" :class="{ selected: node.index === selectedIndex }" @pointerdown="onNodeDown($event, node.index)" @pointermove="onNodeMove" @pointerup="onNodeUp" @pointercancel="onNodeUp" />
    </svg>
    <div v-if="status" class="earth-map-status" role="status"><span>{{ status }}</span><button v-if="failed" type="button" @click="initializeEarthMap">重试</button></div>
  </div>
</template>

<style scoped>
.earth-map { position: absolute; inset: 0; z-index: 1; visibility: hidden; background: #e8edf1; }
.earth-map.active { visibility: visible; }
.earth-map-canvas, .earth-map-overlay { position: absolute; inset: 0; width: 100%; height: 100%; }
.earth-map-overlay { pointer-events: none; overflow: hidden; }
.earth-route-shadow, .earth-route, .earth-weather, .earth-footprint, .earth-rays, .earth-range { fill: none; stroke-linecap: round; stroke-linejoin: round; }
.earth-route-shadow { stroke: #082b2a; stroke-opacity: .75; stroke-width: 10; }
.earth-route { stroke: #00e5a8; stroke-width: 4; }
.earth-weather { stroke-width: 5; stroke-opacity: .8; }
.earth-footprint { fill: #72e6c2; fill-opacity: .28; stroke: #26e7b6; stroke-width: 2; }
.earth-rays { stroke: #ffb547; stroke-width: 2; stroke-dasharray: 5 5; }
.earth-range { stroke-width: 1; }
.earth-range-start { fill: #0f6cbd; fill-opacity: .12; stroke: #0f6cbd; }
.earth-range-end { fill: #5b5fc7; fill-opacity: .12; stroke: #5b5fc7; }
.earth-endpoint circle { stroke: #fff; stroke-width: 3; }
.earth-endpoint text { fill: #fff; stroke: #12343b; stroke-width: 3; paint-order: stroke; text-anchor: middle; font-size: 14px; font-weight: 700; }
.earth-node { fill: #00e5a8; stroke: #fff; stroke-width: 2; pointer-events: all; cursor: pointer; touch-action: none; }
.earth-node.selected { fill: #ffb547; stroke-width: 3; cursor: crosshair; }
.earth-map-status { position: absolute; top: 50%; left: 50%; display: grid; justify-items: center; gap: 9px; max-width: min(320px, 80%); transform: translate(-50%, -50%); padding: 12px 16px; background: #fff; border: 1px solid #d8dde3; color: #424242; text-align: center; font-size: 13px; }
.earth-map-status button { padding: 5px 12px; border: 1px solid #0f6cbd; border-radius: 4px; background: #fff; color: #0f6cbd; cursor: pointer; }
</style>

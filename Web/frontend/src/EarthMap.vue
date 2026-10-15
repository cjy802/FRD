<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { createMapScene, type BaiduScene, type CameraPose, type MapProvider, type SceneView } from './baiduScene'

type Point = { longitude: number; latitude: number }
type AnchorRole = 'start' | 'middle' | 'end'
type Anchor = Point & { id: string; label?: string; role?: AnchorRole }
const props = defineProps<{
  active: boolean; provider: MapProvider; viewMode: '2d' | '3d'; start: Point; end: Point; route: number[][] | null
  anchors?: Anchor[]
  selectedIndex: number | null; editMode: 'route' | 'node'; footprint: number[][]
  weatherSegments: { start: number[]; end: number[]; level: string }[]
  showStartRange: boolean; showEndRange: boolean; rangeMeters: number
  flightPose?: (CameraPose & { follow?: boolean; playing?: boolean }) | null
  flightFootprint?: number[][]
}>()
const emit = defineEmits<{ 'select-waypoint': [index: number]; 'choose-point': [point: Point]; 'rotate-waypoint': [heading: number] }>()
const earthElement = ref<HTMLElement | null>(null)
const status = ref('正在加载地球模式…')
const failed = ref(false)
const tilt = ref(52)
const overlay = reactive({
  nodes: [] as { x: number; y: number; index: number }[],
  groundNodes: [] as { x: number; y: number; index: number }[],
  anchors: [] as { id: string; x: number; y: number; label: string; role: 'start' | 'middle' | 'end' }[],
  autoNodes: [] as { index: number; x: number; y: number }[],
  drone: null as { x: number; y: number; angle: number } | null,
})
let scene: BaiduScene | null = null
let resizeObserver: ResizeObserver | null = null
let unmounted = false
let drag: { index: number; pointerId: number; x: number; y: number; element: HTMLElement; moved: boolean } | null = null
let ctrlRotate: { pointerId: number; x: number; y: number; heading: number; pitch: number } | null = null
let mapPointerStart: { x: number; y: number } | null = null
let mapPointerMoved = false
let routeSignature = ''
let footprintTimer: ReturnType<typeof setTimeout> | null = null
let lastFlightFootprintAt = 0

function anchorPoints(): Anchor[] {
  const valid = props.anchors?.filter(point => Number.isFinite(point.longitude) && Number.isFinite(point.latitude) && Math.abs(point.longitude) <= 180 && Math.abs(point.latitude) <= 90)
  return valid ?? [{ id: 'start', ...props.start }, { id: 'end', ...props.end }]
}
function anchorLabel(index: number): string {
  let value = index + 1
  let label = ''
  while (value > 0) { value--; label = String.fromCharCode(65 + value % 26) + label; value = Math.floor(value / 26) }
  return label
}
function anchorRole(point: Anchor, index: number, count: number): AnchorRole {
  return point.role ?? (index === 0 ? 'start' : index === count - 1 ? 'end' : 'middle')
}
function coordinates() {
  if (props.route?.length) return props.route
  return anchorPoints().map(point => [point.longitude, point.latitude, 0])
}
function circle(point: Point) {
  return Array.from({ length: 64 }, (_, index) => {
    const angle = index * Math.PI / 32
    return [point.longitude + props.rangeMeters / (111320 * Math.cos(point.latitude * Math.PI / 180)) * Math.cos(angle), point.latitude + props.rangeMeters / 111320 * Math.sin(angle), 2]
  })
}
function projectedDrone() {
  const pose = props.flightPose
  if (!scene || !pose || ![pose.longitude, pose.latitude, pose.altitude, pose.heading].every(Number.isFinite)) return null
  const altitude = props.viewMode === '2d' ? 3 : pose.altitude
  const pixel = scene.project([pose.longitude, pose.latitude, altitude])
  if (!pixel) return null
  const heading = pose.heading * Math.PI / 180
  const sampleMetres = 15
  const latitudeDelta = Math.cos(heading) * sampleMetres / 111320
  const longitudeDelta = Math.sin(heading) * sampleMetres / (111320 * Math.max(.01, Math.cos(pose.latitude * Math.PI / 180)))
  const ahead = scene.project([pose.longitude + longitudeDelta, pose.latitude + latitudeDelta, altitude])
  const behind = ahead ? null : scene.project([pose.longitude - longitudeDelta, pose.latitude - latitudeDelta, altitude])
  const dx = ahead ? ahead.x - pixel.x : behind ? pixel.x - behind.x : 1
  const dy = ahead ? ahead.y - pixel.y : behind ? pixel.y - behind.y : 0
  return { x: pixel.x, y: pixel.y, angle: Math.atan2(dy, dx) * 180 / Math.PI }
}
function renderOverlay() {
  if (!scene || !props.active) return
  const majorAnchors = anchorPoints()
  const anchors = majorAnchors.flatMap((point, index, all) => {
    const pixel = scene!.project([point.longitude, point.latitude, 2])
    return pixel ? [{ id: point.id, ...pixel, label: point.label ?? anchorLabel(index), role: anchorRole(point, index, all.length) }] : []
  })
  const route = props.route ?? []
  const stride = Math.max(1, Math.ceil(Math.max(0, route.length - 2) / 120))
  const autoNodes: { index: number; x: number; y: number }[] = []
  if (props.editMode === 'route') {
    for (let index = 1; index < route.length - 1; index += stride) {
      const point = route[index]
      if (majorAnchors.some(anchor => Math.abs(point[0] - anchor.longitude) < 1e-7 && Math.abs(point[1] - anchor.latitude) < 1e-7)) continue
      const pixel = scene.project([point[0], point[1], props.viewMode === '2d' ? 2 : (point[2] ?? 100)])
      if (pixel && anchors.every(anchor => Math.hypot(pixel.x - anchor.x, pixel.y - anchor.y) > 18)) autoNodes.push({ index, ...pixel })
    }
  }
  const nodes = props.editMode === 'node' ? (props.route ?? []).flatMap((point, index) => {
    const pixel = scene!.project([point[0], point[1], props.viewMode === '2d' ? 2 : (point[2] ?? 100)])
    return pixel ? [{ ...pixel, index }] : []
  }) : []
  const groundNodes = props.editMode === 'node' && props.viewMode === '3d' ? (props.route ?? []).flatMap((point, index) => {
    const pixel = scene!.project([point[0], point[1], 1])
    return pixel ? [{ ...pixel, index }] : []
  }) : []
  if (JSON.stringify(nodes) !== JSON.stringify(overlay.nodes)) overlay.nodes = nodes
  if (JSON.stringify(groundNodes) !== JSON.stringify(overlay.groundNodes)) overlay.groundNodes = groundNodes
  if (JSON.stringify(anchors) !== JSON.stringify(overlay.anchors)) overlay.anchors = anchors
  if (JSON.stringify(autoNodes) !== JSON.stringify(overlay.autoNodes)) overlay.autoNodes = autoNodes
  const drone = projectedDrone()
  if (JSON.stringify(drone) !== JSON.stringify(overlay.drone)) overlay.drone = drone
}
function refreshRoute() {
  if (!scene) return
  const anchors = anchorPoints()
  const start = anchors.find((point, index) => anchorRole(point, index, anchors.length) === 'start')
  const end = anchors.find((point, index) => anchorRole(point, index, anchors.length) === 'end')
  scene.setRoute(props.route ?? [], props.viewMode, props.weatherSegments, [
    ...(props.showStartRange && start ? [circle(start)] : []), ...(props.showEndRange && end ? [circle(end)] : []),
  ])
  const signature = JSON.stringify(coordinates().map(point => point.slice(0, 2)))
  if (!props.flightPose?.follow && (!routeSignature || (signature !== routeSignature && props.editMode === 'route'))) scene.fit(coordinates(), props.viewMode)
  routeSignature = signature
  refreshFootprint()
}
function refreshFootprint() {
  if (!scene) return
  if (footprintTimer !== null) { clearTimeout(footprintTimer); footprintTimer = null }
  const apply = () => {
    footprintTimer = null
    if (!scene) return
    const pose = props.flightPose
    const origin = pose
      ? [pose.longitude, pose.latitude, pose.altitude]
      : props.editMode === 'node' && props.selectedIndex !== null ? props.route?.[props.selectedIndex] ?? null : null
    scene.setFootprint(origin, pose ? props.flightFootprint ?? [] : props.footprint, props.viewMode)
    lastFlightFootprintAt = performance.now()
  }
  if (!props.flightPose) { apply(); return }
  const wait = Math.max(0, 120 - (performance.now() - lastFlightFootprintAt))
  if (!wait) apply()
  else footprintTimer = setTimeout(apply, wait)
}
function applyViewMode() {
  scene?.setView(props.viewMode, coordinates())
  tilt.value = props.viewMode === '2d' ? 0 : 52
  refreshRoute()
  if (props.flightPose?.follow) scene?.followAt(props.flightPose, props.viewMode)
  renderOverlay()
}
function changeTilt(event: Event) { tilt.value = Number((event.target as HTMLInputElement).value); scene?.setTilt(tilt.value) }
function focusWaypoint(index: number) { const point = props.route?.[index]; if (point) scene?.focus(point, props.viewMode) }
function setView(view: SceneView) { scene?.setView(view, coordinates()) }
defineExpose({ focusWaypoint, setView })

function onMapPointerDown(event: PointerEvent) { mapPointerStart = { x: event.clientX, y: event.clientY }; mapPointerMoved = false }
function onMapPointerMove(event: PointerEvent) { if (mapPointerStart && Math.hypot(event.clientX - mapPointerStart.x, event.clientY - mapPointerStart.y) > 5) mapPointerMoved = true }
function onCtrlRotateDown(event: PointerEvent) {
  if (!scene || !props.active || props.flightPose?.playing || props.viewMode !== '3d' || ctrlRotate || event.button !== 0 || !event.isPrimary || !event.ctrlKey ||
      !earthElement.value?.contains(event.target as Node)) return
  event.preventDefault()
  event.stopImmediatePropagation()
  finishDrag()
  const { heading, pitch } = scene.getViewAngles()
  ctrlRotate = { pointerId: event.pointerId, x: event.clientX, y: event.clientY, heading, pitch }
  mapPointerStart = null
  mapPointerMoved = true
  scene.setInputs(false)
  window.addEventListener('pointermove', onCtrlRotateMove, true)
  window.addEventListener('pointerup', onCtrlRotateEnd, true)
  window.addEventListener('pointercancel', onCtrlRotateEnd, true)
  window.addEventListener('blur', finishCtrlRotate)
}
function onCtrlMouseDown(event: MouseEvent) {
  if (!ctrlRotate || event.button !== 0) return
  event.preventDefault()
  event.stopImmediatePropagation()
}
function onCtrlRotateMove(event: PointerEvent) {
  const rotation = ctrlRotate
  if (!rotation || event.pointerId !== rotation.pointerId || !scene) return
  // Engine headings increase counterclockwise; invert the deltas so the view follows the drag.
  const heading = rotation.heading - (event.clientX - rotation.x) * 0.4
  const pitch = Math.max(0, Math.min(75, rotation.pitch - (event.clientY - rotation.y) * 0.3))
  scene.setViewAngles(heading, pitch)
  tilt.value = Math.round(pitch)
  event.preventDefault()
}
function onCtrlRotateEnd(event: PointerEvent) {
  if (event.pointerId === ctrlRotate?.pointerId) finishCtrlRotate()
}
function finishCtrlRotate() {
  if (!ctrlRotate) return
  ctrlRotate = null
  window.removeEventListener('pointermove', onCtrlRotateMove, true)
  window.removeEventListener('pointerup', onCtrlRotateEnd, true)
  window.removeEventListener('pointercancel', onCtrlRotateEnd, true)
  window.removeEventListener('blur', finishCtrlRotate)
  scene?.setInputs(props.active && !props.flightPose?.playing)
}
function onMapClick(event: MouseEvent) {
  if (!scene || !props.active || props.flightPose?.playing || props.editMode !== 'route' || mapPointerMoved || !earthElement.value) return
  const bounds = earthElement.value.getBoundingClientRect()
  const point = scene.pick(event.clientX - bounds.left, event.clientY - bounds.top)
  if (point) emit('choose-point', { longitude: Number(point[0].toFixed(5)), latitude: Number(point[1].toFixed(5)) })
}
function onNodeDown(event: PointerEvent, index: number) {
  if (event.button !== 0 || !event.isPrimary || !props.active || props.flightPose?.playing) return
  event.preventDefault(); event.stopPropagation()
  finishDrag()
  emit('select-waypoint', index)
  const element = event.currentTarget as HTMLElement
  drag = { index, pointerId: event.pointerId, x: event.clientX, y: event.clientY, element, moved: false }
  element.setPointerCapture(event.pointerId)
  scene?.setInputs(false)
}
function onNodeMove(event: PointerEvent) {
  if (!drag || drag.pointerId !== event.pointerId || !scene || !earthElement.value || !props.route?.[drag.index]) return
  if (!drag.moved && Math.hypot(event.clientX - drag.x, event.clientY - drag.y) < 5) return
  drag.moved = true
  const bounds = earthElement.value.getBoundingClientRect()
  const target = scene.pick(event.clientX - bounds.left, event.clientY - bounds.top)
  if (!target) return
  const origin = props.route[drag.index], lat1 = origin[1] * Math.PI / 180, lat2 = target[1] * Math.PI / 180, longitude = (target[0] - origin[0]) * Math.PI / 180
  emit('rotate-waypoint', Math.atan2(Math.sin(longitude) * Math.cos(lat2), Math.cos(lat1) * Math.sin(lat2) - Math.sin(lat1) * Math.cos(lat2) * Math.cos(longitude)) * 180 / Math.PI)
}
function finishDrag() {
  const previous = drag; drag = null
  if (previous?.element.hasPointerCapture(previous.pointerId)) previous.element.releasePointerCapture(previous.pointerId)
  scene?.setInputs(props.active && !props.flightPose?.playing)
  mapPointerStart = null
}
function onNodeUp(event: PointerEvent) { if (event.pointerId === drag?.pointerId) finishDrag() }
function onNodeClick(index: number) { if (!props.flightPose?.playing) emit('select-waypoint', index) }
async function initialize() {
  status.value = props.provider === 'baidu' ? '正在加载地球模式…' : '正在加载地图模式…'; failed.value = false
  try {
    // The engine needs a center on startup, even when all user anchors are blank.
    const nextScene = await createMapScene(earthElement.value!, coordinates()[0] ?? [props.start.longitude, props.start.latitude, 0], props.provider)
    if (unmounted) { nextScene.dispose(); return }
    scene = nextScene
    scene.onFrame(renderOverlay)
    applyViewMode()
    scene.setActive(props.active)
    scene.setInputs(props.active && !props.flightPose?.playing)
    resizeObserver = new ResizeObserver(() => scene?.resize())
    resizeObserver.observe(earthElement.value!)
    status.value = ''
  } catch (error) {
    scene?.dispose(); scene = null; resizeObserver?.disconnect(); resizeObserver = null
    status.value = error instanceof Error ? error.message : '地球模式加载失败。'; failed.value = true
  }
}
onMounted(() => {
  window.addEventListener('pointerup', finishDrag); window.addEventListener('pointercancel', finishDrag); window.addEventListener('blur', finishDrag)
  void initialize()
})
watch(() => props.active, async active => { finishDrag(); finishCtrlRotate(); await nextTick(); scene?.setActive(active); scene?.setInputs(active && !props.flightPose?.playing); if (active) { scene?.resize(); renderOverlay() } })
watch(() => props.viewMode, () => { finishDrag(); finishCtrlRotate(); applyViewMode() })
watch(() => props.provider, async () => {
  finishDrag()
  finishCtrlRotate()
  scene?.dispose(); scene = null
  await nextTick()
  if (!unmounted) void initialize()
})
watch(() => [props.route, props.start, props.end, props.anchors, props.weatherSegments, props.showStartRange, props.showEndRange, props.rangeMeters], refreshRoute, { deep: true })
watch(() => [props.footprint, props.selectedIndex, props.editMode], () => { refreshFootprint(); renderOverlay() }, { deep: true, flush: 'sync' })
watch(() => props.flightPose, (pose, previous) => {
  if (pose?.playing) { finishDrag(); finishCtrlRotate() }
  scene?.setInputs(props.active && !pose?.playing)
  if (pose?.follow && scene && props.active) scene.followAt(pose, props.viewMode)
  // Leaving the simulation returns the camera to the whole-route overview.
  if (!pose && previous && scene) scene.fit(coordinates(), props.viewMode)
  refreshFootprint()
  renderOverlay()
}, { deep: true, flush: 'sync' })
watch(() => props.flightFootprint, refreshFootprint, { deep: true, flush: 'sync' })
onBeforeUnmount(() => {
  unmounted = true; finishDrag(); finishCtrlRotate(); resizeObserver?.disconnect()
  if (footprintTimer !== null) clearTimeout(footprintTimer)
  window.removeEventListener('pointerup', finishDrag); window.removeEventListener('pointercancel', finishDrag); window.removeEventListener('blur', finishDrag)
  scene?.dispose(); scene = null
})
</script>

<template>
  <div class="earth-map" :class="{ active }" :aria-label="provider === 'baidu' ? '百度地图地球模式' : 'OpenStreetMap 地图模式'" :data-view-mode="viewMode" @pointerdown.capture="onCtrlRotateDown" @mousedown.capture="onCtrlMouseDown">
    <div ref="earthElement" class="earth-map-canvas" @pointerdown="onMapPointerDown" @pointermove="onMapPointerMove" @click="onMapClick"></div>
    <div class="earth-map-overlay">
      <span v-for="anchor in overlay.anchors" :key="anchor.id" class="earth-endpoint" :class="anchor.role" :style="{ left: `${anchor.x}px`, top: `${anchor.y}px` }">{{ anchor.label }}</span>
      <span v-for="node in overlay.autoNodes" :key="`auto-${node.index}`" class="earth-auto-node" aria-hidden="true" :style="{ left: `${node.x}px`, top: `${node.y}px` }"></span>
      <span v-for="node in overlay.groundNodes" :key="`ground-${node.index}`" class="earth-ground-node" aria-hidden="true" :style="{ left: `${node.x}px`, top: `${node.y}px` }"><span></span></span>
      <button v-for="node in overlay.nodes" :key="node.index" type="button" class="earth-node" :class="{ selected: node.index === selectedIndex }" :aria-label="`选择航点 ${node.index + 1}`" :aria-pressed="node.index === selectedIndex" :data-waypoint-index="node.index" :disabled="flightPose?.playing" :style="{ left: `${node.x}px`, top: `${node.y}px` }" @pointerdown="onNodeDown($event, node.index)" @pointermove="onNodeMove" @pointerup="onNodeUp" @pointercancel="onNodeUp" @lostpointercapture="onNodeUp" @click.stop="onNodeClick(node.index)"><span></span></button>
      <span v-if="overlay.drone" class="earth-flight-drone" role="img" aria-label="模拟无人机位置" :style="{ left: `${overlay.drone.x}px`, top: `${overlay.drone.y}px`, transform: `translate(-50%, -50%) rotate(${overlay.drone.angle}deg)` }"><img src="/svg/jiantou.svg" alt="" /></span>
    </div>
    <label v-if="viewMode === '3d' && !status" class="earth-tilt-control">观察角度 <input aria-label="地球 3D 观察角度" type="range" min="0" max="75" :value="tilt" @input="changeTilt" /><span>{{ tilt }}°</span></label>
    <div v-if="status" class="earth-map-status" role="status"><span>{{ status }}</span><button v-if="failed" type="button" @click="initialize">重试</button></div>
    <span class="earth-attribution">{{ provider === 'baidu' ? '© 百度地图' : '© OpenStreetMap contributors' }}</span>
  </div>
</template>

<style scoped>
.earth-map { position: absolute; inset: 0; z-index: 1; visibility: hidden; background: #e8edf1; }
.earth-map.active { visibility: visible; }
.earth-map-canvas, .earth-map-overlay { position: absolute; inset: 0; width: 100%; height: 100%; }
.earth-map-canvas { z-index: 0; }
.earth-map-overlay { z-index: 1; pointer-events: none; overflow: hidden; }
.earth-node { position: absolute; width: 32px; height: 32px; transform: translate(-50%, -50%); display: grid; place-items: center; padding: 0; border: 0; border-radius: 50%; background: transparent; pointer-events: auto; cursor: pointer; touch-action: none; }
.earth-node:hover:not(:disabled), .earth-node:active:not(:disabled) { transform: translate(-50%, -50%); box-shadow: none; }
.earth-node span { width: 12px; height: 12px; border: 2px solid #fff; border-radius: 50%; background: #00e5a8; box-shadow: 0 1px 5px #12343b; }
.earth-node.selected span { width: 18px; height: 18px; background: #ffb547; }
.earth-node:focus-visible { outline: 2px solid #ffb547; }
.earth-ground-node { position: absolute; width: 14px; height: 14px; transform: translate(-50%, -50%); display: grid; place-items: center; pointer-events: none; }
.earth-ground-node span { width: 8px; height: 8px; border: 2px solid #fff; border-radius: 50%; background: #36dfba; box-shadow: 0 1px 4px #12343b; }
.earth-endpoint { position: absolute; transform: translate(-50%, -100%); padding: 3px 6px; border-radius: 4px; color: #fff; font-size: 12px; font-weight: 700; }
.earth-endpoint.start { background: #0f6cbd; }
.earth-endpoint.middle { background: #197b78; }
.earth-endpoint.end { background: #5b5fc7; }
.earth-auto-node { position: absolute; width: 6px; height: 6px; transform: translate(-50%, -50%); border: 1px solid #fff; border-radius: 50%; background: #00aa83; box-shadow: 0 1px 3px rgb(18 52 59 / .5); pointer-events: none; }
.earth-flight-drone { position: absolute; z-index: 3; width: 40px; height: 40px; display: grid; place-items: center; border: 2px solid #fff; border-radius: 50%; background: #126cb7; box-shadow: 0 0 0 5px rgb(18 108 183 / .19), 0 4px 14px rgb(9 42 74 / .45); pointer-events: none; will-change: left, top, transform; }
.earth-flight-drone img { width: 28px; height: 28px; display: block; }
.earth-tilt-control { position: absolute; z-index: 2; right: 16px; top: 80px; display: flex; align-items: center; gap: 8px; min-height: 38px; padding: 0 12px; background: #fff; border: 1px solid #d8e0e7; border-radius: 8px; box-shadow: 0 4px 14px rgb(23 50 77 / .14); color: #344455; font-size: 12px; }
.earth-tilt-control input { width: 90px; }
.earth-map-status { position: absolute; z-index: 2; top: 50%; left: 50%; display: grid; justify-items: center; gap: 9px; max-width: min(320px, 80%); transform: translate(-50%, -50%); padding: 12px 16px; background: #fff; border: 1px solid #d8dde3; color: #424242; text-align: center; font-size: 13px; }
.earth-map-status button { padding: 5px 12px; border: 1px solid #0f6cbd; border-radius: 4px; background: #fff; color: #0f6cbd; cursor: pointer; }
.earth-attribution { position: absolute; bottom: 4px; right: 6px; color: #fff; text-shadow: 0 1px 2px #000; font-size: 11px; }
@media (max-width: 900px) { .earth-tilt-control { top: 164px; right: 10px; } }
</style>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { createMapScene, type BaiduScene, type CameraPose, type MapProvider, type SceneView } from './baiduScene'
import type { VideoCaptureTarget, VideoTelemetryPoint } from './videoSurvey'
import type { RouteAssessment, WeatherRiskPoint } from './weatherTypes'

type Point = { longitude: number; latitude: number }
type AnchorRole = 'start' | 'middle' | 'end'
type Anchor = Point & { id: string; label?: string; role?: AnchorRole }
type OverlayPoint = { x: number; y: number; index?: number; id?: string; label?: string; role?: AnchorRole; selected?: boolean }
type CoverageVertex = { index: number; x: number; y: number }
type WeatherOverlayPoint = {
  index: number; x: number; y: number; popupX: number; popupY: number; placement: 'above' | 'below'
  time: string; level: string; riskLabel: string; reasons: string; coordinateLabel: string
  temperature: string; groundWind: string; heightWind: string; gust: string; visibility: string; rain: string
}
const props = defineProps<{
  active: boolean; provider: MapProvider; viewMode: '2d' | '3d'; start: Point; end: Point; route: number[][] | null
  anchors?: Anchor[]
  selectedAnchorId?: string
  coveragePolygon?: number[][]
  coverageDrawEnabled?: boolean
  coverageEditEnabled?: boolean
  suppressMapPointSelection?: boolean
  selectedIndex: number | null; editMode: 'route' | 'node'; footprint: number[][]
  weatherSegments: { start: number[]; end: number[]; level: string }[]
  weatherSamples?: RouteAssessment['samples'] | null
  weatherRiskPoints?: WeatherRiskPoint[] | null
  weatherAltitudeM?: number
  showStartRange: boolean; showEndRange: boolean; rangeMeters: number
  flightPose?: (CameraPose & { follow?: boolean; playing?: boolean }) | null
  flightFootprint?: number[][]
  videoTrack?: number[][]
  videoPosition?: VideoTelemetryPoint | null
  captureTargets?: VideoCaptureTarget[]
}>()
const emit = defineEmits<{
  'select-waypoint': [index: number]
  'choose-point': [point: Point]
  'rotate-waypoint': [heading: number]
  'coverage-polygon-created': [polygon: number[][]]
  'coverage-polygon-updated': [polygon: number[][]]
}>()
const earthElement = ref<HTMLElement | null>(null)
const status = ref('正在加载地球模式…')
const failed = ref(false)
const tilt = ref(52)
const selectedWeatherPointIndex = ref<number | null>(null)
const overlay = reactive({
  nodes: [] as { x: number; y: number; index: number }[],
  groundNodes: [] as { x: number; y: number; index: number }[],
  anchors: [] as { id: string; x: number; y: number; label: string; role: 'start' | 'middle' | 'end'; selected: boolean }[],
  coverageVertices: [] as CoverageVertex[],
  autoNodes: [] as { index: number; x: number; y: number }[],
  drone: null as { x: number; y: number; angle: number } | null,
  videoPosition: null as { x: number; y: number; time_seconds: number; altitude_m: number | null } | null,
  captureTargets: [] as { id: string; x: number; y: number; label: string; marker: string; time_seconds: number }[],
  weatherPoints: [] as WeatherOverlayPoint[],
})
let scene: BaiduScene | null = null
let resizeObserver: ResizeObserver | null = null
let unmounted = false
let drag: { index: number; pointerId: number; x: number; y: number; element: HTMLElement; moved: boolean } | null = null
let ctrlRotate: { pointerId: number; x: number; y: number; heading: number; pitch: number } | null = null
let mapPointerStart: { x: number; y: number } | null = null
let mapPointerMoved = false
let coverageDraw: { pointerId: number; start: number[]; startX: number; startY: number; element: HTMLElement } | null = null
let coverageVertexDrag: { index: number; pointerId: number; element: HTMLElement } | null = null
let coverageDraft: number[][] | null = null
let routeSignature = ''
let footprintTimer: ReturnType<typeof setTimeout> | null = null
let lastFlightFootprintAt = 0
const selectedWeatherPoint = computed(() => overlay.weatherPoints.find(point => point.index === selectedWeatherPointIndex.value) ?? null)
const coverageSvgPoints = computed(() => overlay.coverageVertices.map(point => `${point.x},${point.y}`).join(' '))
const coverageMidpoints = computed(() => overlay.coverageVertices.map((point, index, points) => {
  const next = points[(index + 1) % points.length]
  return { index, x: (point.x + next.x) / 2, y: (point.y + next.y) / 2 }
}))

function sameOverlayPoints(first: OverlayPoint[], second: OverlayPoint[]) {
  if (first.length !== second.length) return false
  return first.every((point, index) => {
    const previous = second[index]
    return point.x === previous.x && point.y === previous.y
      && point.index === previous.index && point.id === previous.id
      && point.label === previous.label && point.role === previous.role && point.selected === previous.selected
  })
}

function sameDrone(first: { x: number; y: number; angle: number } | null, second: { x: number; y: number; angle: number } | null) {
  return first === second || (!!first && !!second && first.x === second.x && first.y === second.y && first.angle === second.angle)
}

function sameVideoPosition(first: typeof overlay.videoPosition, second: typeof overlay.videoPosition) {
  return first === second || (!!first && !!second && first.x === second.x && first.y === second.y
    && first.time_seconds === second.time_seconds && first.altitude_m === second.altitude_m)
}

function sameWeatherPoints(first: WeatherOverlayPoint[], second: WeatherOverlayPoint[]) {
  if (first.length !== second.length) return false
  return first.every((point, index) => {
    const previous = second[index]
    return point.index === previous.index && point.x === previous.x && point.y === previous.y
      && point.popupX === previous.popupX && point.popupY === previous.popupY && point.placement === previous.placement
      && point.time === previous.time && point.level === previous.level && point.riskLabel === previous.riskLabel
      && point.reasons === previous.reasons && point.coordinateLabel === previous.coordinateLabel && point.temperature === previous.temperature
      && point.groundWind === previous.groundWind && point.heightWind === previous.heightWind
      && point.gust === previous.gust && point.visibility === previous.visibility && point.rain === previous.rain
  })
}

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
    return pixel ? [{ id: point.id, ...pixel, label: point.label ?? anchorLabel(index), role: anchorRole(point, index, all.length), selected: point.id === props.selectedAnchorId }] : []
  })
  const coverageSource = coverageDraft ?? props.coveragePolygon ?? []
  const projectedCoverage = coverageSource.map((point, index) => {
    const pixel = scene!.project([point[0]!, point[1]!, 2])
    return pixel ? { ...pixel, index } : null
  })
  const coverageVertices = projectedCoverage.length >= 3 && projectedCoverage.every((point) => point !== null)
    ? projectedCoverage as CoverageVertex[] : []
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
  const position = props.videoPosition && Number.isFinite(props.videoPosition.longitude) && Number.isFinite(props.videoPosition.latitude)
    ? scene.project([props.videoPosition.longitude, props.videoPosition.latitude,
      props.viewMode === '2d' ? 2 : (props.videoPosition.altitude_m ?? 2)])
    : null
  const videoPosition = position && props.videoPosition
    ? { ...position, time_seconds: props.videoPosition.time_seconds, altitude_m: props.videoPosition.altitude_m }
    : null
  const captureTargets = (props.captureTargets ?? []).flatMap((target, index) => {
    const pixel = scene!.project([target.longitude, target.latitude, props.viewMode === '2d' ? 2 : (target.altitude_m ?? 2)])
    const label = target.label || `近拍点 ${index + 1}`
    const marker = label.match(/候选\s*(\d+)/)?.[1] ?? (label.replace(/[^0-9]/g, '').slice(0, 2) || '拍')
    return pixel ? [{ ...pixel, id: target.id, label, marker, time_seconds: target.time_seconds }] : []
  })
  const riskByIndex = new Map((props.weatherRiskPoints ?? []).map(point => [point.index, point]))
  const mapWidth = earthElement.value?.clientWidth ?? 0
  const mapHeight = earthElement.value?.clientHeight ?? 0
  const popupWidth = Math.min(258, Math.max(0, mapWidth - 16))
  const popupHeight = 190
  const weatherPoints = (props.weatherSamples ?? []).flatMap(sample => {
    const routePoint = props.route?.[sample.index]
    const altitude = props.viewMode === '2d' ? 3 : (routePoint?.[2] ?? props.weatherAltitudeM ?? 100)
    const pixel = scene!.project([sample.coordinate[0], sample.coordinate[1], altitude])
    if (!pixel) return []
    const pointRisk = riskByIndex.get(sample.index)
    const level = pointRisk?.level ?? 'no_trigger'
    const reasons = pointRisk?.triggers.map(trigger => trigger.reason).join('；') ?? ''
    const visibility = sample.height_weather?.visibility_surface_m
    const popupX = mapWidth > popupWidth + 16
      ? Math.max(popupWidth / 2 + 8, Math.min(mapWidth - popupWidth / 2 - 8, pixel.x)) : mapWidth / 2
    const placement: WeatherOverlayPoint['placement'] = pixel.y < popupHeight + 24 ? 'below' : 'above'
    const popupY = placement === 'below'
      ? Math.max(8, Math.min(mapHeight - popupHeight - 8, pixel.y + 18))
      : Math.max(popupHeight + 8, Math.min(mapHeight - 8, pixel.y - 16))
    return [{
      index: sample.index,
      ...pixel,
      popupX,
      popupY,
      placement,
      time: new Date(sample.eta_utc).toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', hour12: false }),
      level,
      riskLabel: level === 'elevated' ? '较强提醒' : level === 'attention' ? '需要留意' : level === 'incomplete' ? '资料缺测' : '未触发提醒',
      reasons,
      coordinateLabel: `${sample.coordinate[1].toFixed(5)}, ${sample.coordinate[0].toFixed(5)}`,
      temperature: `${(sample.weather.values.t2m_K - 273.15).toFixed(1)}°C`,
      groundWind: `${sample.weather.derived.wind_speed_10m_m_s.toFixed(1)} m/s`,
      heightWind: sample.height_weather ? `${sample.height_weather.wind_speed_m_s.toFixed(1)} m/s` : '缺测',
      gust: sample.height_weather ? `${sample.height_weather.gust_surface_m_s.toFixed(1)} m/s` : '缺测',
      visibility: visibility === undefined || visibility === null ? '缺测' : `${(visibility / 1000).toFixed(1)} km`,
      rain: `${sample.weather.values.tp_6h_mm.toFixed(2)} mm/6h`,
    }]
  })
  if (!sameOverlayPoints(nodes, overlay.nodes)) overlay.nodes = nodes
  if (!sameOverlayPoints(groundNodes, overlay.groundNodes)) overlay.groundNodes = groundNodes
  if (!sameOverlayPoints(anchors, overlay.anchors)) overlay.anchors = anchors
  if (!sameOverlayPoints(coverageVertices, overlay.coverageVertices)) overlay.coverageVertices = coverageVertices
  if (!sameOverlayPoints(autoNodes, overlay.autoNodes)) overlay.autoNodes = autoNodes
  const drone = projectedDrone()
  if (!sameDrone(drone, overlay.drone)) overlay.drone = drone
  if (!sameVideoPosition(videoPosition, overlay.videoPosition)) overlay.videoPosition = videoPosition
  if (!sameOverlayPoints(captureTargets, overlay.captureTargets)) overlay.captureTargets = captureTargets
  if (!sameWeatherPoints(weatherPoints, overlay.weatherPoints)) overlay.weatherPoints = weatherPoints
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
  renderOverlay()
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
function refreshVideoTrack() {
  if (!scene) return
  const track = (props.videoTrack ?? []).filter(point => point.length >= 2
    && Number.isFinite(point[0]) && Number.isFinite(point[1])
    && Math.abs(point[0]) <= 180 && Math.abs(point[1]) <= 90)
  scene.setTelemetryTrack(track)
  if (track.length > 1) scene.fit(track, props.viewMode)
  renderOverlay()
}
function applyViewMode() {
  scene?.setView(props.viewMode, coordinates())
  tilt.value = props.viewMode === '2d' ? 0 : 52
  refreshRoute()
  refreshVideoTrack()
  if (props.flightPose?.follow) scene?.followAt(props.flightPose, props.viewMode)
  renderOverlay()
}
function changeTilt(event: Event) { tilt.value = Number((event.target as HTMLInputElement).value); scene?.setTilt(tilt.value) }
function focusWaypoint(index: number) { const point = props.route?.[index]; if (point) scene?.focus(point, props.viewMode) }
function setView(view: SceneView) { scene?.setView(view, coordinates()) }
defineExpose({ focusWaypoint, setView })

function pickCoordinate(clientX: number, clientY: number): number[] | null {
  if (!scene || !earthElement.value) return null
  const bounds = earthElement.value.getBoundingClientRect()
  const point = scene.pick(clientX - bounds.left, clientY - bounds.top)
  return point ? [Number(point[0].toFixed(7)), Number(point[1].toFixed(7))] : null
}

function rectanglePolygon(first: number[], second: number[]) {
  const west = Math.min(first[0]!, second[0]!)
  const east = Math.max(first[0]!, second[0]!)
  const south = Math.min(first[1]!, second[1]!)
  const north = Math.max(first[1]!, second[1]!)
  return [[west, south], [east, south], [east, north], [west, north]]
}

function onMapPointerDown(event: PointerEvent) {
  mapPointerStart = { x: event.clientX, y: event.clientY }
  mapPointerMoved = false
  if (!props.coverageDrawEnabled || !props.active || props.flightPose?.playing || event.button !== 0 || !event.isPrimary || !earthElement.value) return
  const point = pickCoordinate(event.clientX, event.clientY)
  if (!point) return
  event.preventDefault()
  event.stopPropagation()
  const element = event.currentTarget as HTMLElement
  coverageDraw = { pointerId: event.pointerId, start: point, startX: event.clientX, startY: event.clientY, element }
  coverageDraft = rectanglePolygon(point, point)
  mapPointerMoved = true
  scene?.setInputs(false)
  element.setPointerCapture(event.pointerId)
  renderOverlay()
}

function onMapPointerMove(event: PointerEvent) {
  if (coverageDraw?.pointerId === event.pointerId) {
    const point = pickCoordinate(event.clientX, event.clientY)
    if (point) coverageDraft = rectanglePolygon(coverageDraw.start, point)
    renderOverlay()
    event.preventDefault()
    return
  }
  if (mapPointerStart && Math.hypot(event.clientX - mapPointerStart.x, event.clientY - mapPointerStart.y) > 5) mapPointerMoved = true
}

function onMapPointerUp(event: PointerEvent) {
  const drawing = coverageDraw
  if (!drawing || drawing.pointerId !== event.pointerId) return
  const point = pickCoordinate(event.clientX, event.clientY) ?? drawing.start
  const polygon = rectanglePolygon(drawing.start, point)
  const largeEnough = Math.hypot(event.clientX - drawing.startX, event.clientY - drawing.startY) >= 12
  coverageDraw = null
  coverageDraft = null
  mapPointerStart = null
  mapPointerMoved = true
  if (drawing.element.hasPointerCapture(event.pointerId)) drawing.element.releasePointerCapture(event.pointerId)
  scene?.setInputs(props.active && !props.flightPose?.playing)
  if (largeEnough) emit('coverage-polygon-created', polygon)
  renderOverlay()
}

function onCoverageVertexDown(event: PointerEvent, index: number) {
  if (!props.coverageEditEnabled || event.button !== 0 || !event.isPrimary || props.flightPose?.playing || !earthElement.value) return
  event.preventDefault()
  event.stopPropagation()
  finishDrag()
  const element = event.currentTarget as HTMLElement
  coverageDraft = (props.coveragePolygon ?? []).map(point => [...point])
  coverageVertexDrag = { index, pointerId: event.pointerId, element }
  mapPointerStart = null
  mapPointerMoved = true
  scene?.setInputs(false)
  element.setPointerCapture(event.pointerId)
}

function onCoverageVertexMove(event: PointerEvent) {
  const drag = coverageVertexDrag
  if (!drag || drag.pointerId !== event.pointerId || !coverageDraft || !coverageDraft[drag.index]) return
  const point = pickCoordinate(event.clientX, event.clientY)
  if (!point) return
  coverageDraft[drag.index] = point
  renderOverlay()
  event.preventDefault()
}

function onCoverageVertexEnd(event: PointerEvent) {
  const drag = coverageVertexDrag
  if (!drag || drag.pointerId !== event.pointerId) return
  const updated = coverageDraft?.map(point => [...point])
  const original = props.coveragePolygon ?? []
  const changed = !!updated && updated.length === original.length
    && updated.some((point, index) => point.some((coordinate, axis) => coordinate !== original[index]?.[axis]))
  coverageVertexDrag = null
  coverageDraft = null
  if (drag.element.hasPointerCapture(event.pointerId)) drag.element.releasePointerCapture(event.pointerId)
  scene?.setInputs(props.active && !props.flightPose?.playing)
  if (updated && changed) emit('coverage-polygon-updated', updated)
  renderOverlay()
}

function insertCoverageVertex(edgeIndex: number) {
  if (!props.coverageEditEnabled) return
  const polygon = (props.coveragePolygon ?? []).map(point => [...point])
  if (polygon.length < 3) return
  const start = polygon[edgeIndex]
  const end = polygon[(edgeIndex + 1) % polygon.length]
  if (!start || !end) return
  polygon.splice(edgeIndex + 1, 0, [(start[0]! + end[0]!) / 2, (start[1]! + end[1]!) / 2])
  emit('coverage-polygon-updated', polygon)
}

function deleteCoverageVertex(event: MouseEvent, index: number) {
  if (!props.coverageEditEnabled || (props.coveragePolygon?.length ?? 0) <= 3) return
  event.preventDefault()
  event.stopPropagation()
  emit('coverage-polygon-updated', props.coveragePolygon!.filter((_, pointIndex) => pointIndex !== index).map(point => [...point]))
}
function onCtrlRotateDown(event: PointerEvent) {
  if (!scene || !props.active || props.coverageDrawEnabled || props.flightPose?.playing || props.viewMode !== '3d' || ctrlRotate || event.button !== 0 || !event.isPrimary || !event.ctrlKey ||
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
  if (!scene || !props.active || props.flightPose?.playing || props.editMode !== 'route'
    || props.coverageDrawEnabled || props.suppressMapPointSelection || mapPointerMoved || !earthElement.value) return
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
    if (props.coveragePolygon && props.coveragePolygon.length >= 3) {
      scene.fit(props.coveragePolygon.map(([longitude, latitude]) => [longitude, latitude, 0]), props.viewMode)
    }
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
watch(() => [props.coveragePolygon, props.coverageDrawEnabled], () => {
  renderOverlay()
}, { deep: true, flush: 'sync' })
watch(() => props.coverageEditEnabled, enabled => {
  if (enabled || (!coverageDraw && !coverageVertexDrag && !coverageDraft)) return
  // Leaving area mode cancels an in-flight coverage drag; the draft would bypass the filtered coveragePolygon prop and linger on the map.
  coverageDraw = null
  coverageVertexDrag = null
  coverageDraft = null
  scene?.setInputs(props.active && !props.flightPose?.playing)
  renderOverlay()
}, { flush: 'sync' })
watch(() => props.selectedAnchorId, renderOverlay, { flush: 'sync' })
watch(() => [props.footprint, props.selectedIndex, props.editMode], () => { refreshFootprint(); renderOverlay() }, { deep: true, flush: 'sync' })
watch(() => props.videoTrack, refreshVideoTrack, { deep: true })
watch(() => [props.videoPosition, props.captureTargets], renderOverlay, { deep: true, flush: 'sync' })
watch(() => [props.weatherSamples, props.weatherRiskPoints], () => {
  selectedWeatherPointIndex.value = null
  renderOverlay()
}, { deep: true, flush: 'sync' })
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
  unmounted = true; finishDrag(); finishCtrlRotate(); coverageDraw = null; coverageVertexDrag = null; coverageDraft = null; resizeObserver?.disconnect()
  if (footprintTimer !== null) clearTimeout(footprintTimer)
  window.removeEventListener('pointerup', finishDrag); window.removeEventListener('pointercancel', finishDrag); window.removeEventListener('blur', finishDrag)
  scene?.dispose(); scene = null
})
</script>

<template>
  <div class="earth-map" :class="{ active, 'coverage-drawing': coverageDrawEnabled }" :aria-label="provider === 'baidu' ? '百度地图地球模式' : 'OpenStreetMap 地图模式'" :data-view-mode="viewMode" @pointerdown.capture="onCtrlRotateDown" @mousedown.capture="onCtrlMouseDown">
    <div ref="earthElement" class="earth-map-canvas" @pointerdown="onMapPointerDown" @pointermove="onMapPointerMove" @pointerup="onMapPointerUp" @pointercancel="onMapPointerUp" @click="onMapClick"></div>
    <div class="earth-map-overlay">
      <svg v-if="overlay.coverageVertices.length >= 3" class="earth-coverage-svg" aria-hidden="true"><polygon :points="coverageSvgPoints" /></svg>
      <template v-if="coverageEditEnabled && !coverageDrawEnabled">
        <button v-for="midpoint in coverageMidpoints" :key="`coverage-edge-${midpoint.index}`" type="button" class="earth-coverage-edge-handle" :aria-label="`在区域边界 ${midpoint.index + 1} 添加节点`" title="点击添加边界节点" :style="{ left: `${midpoint.x}px`, top: `${midpoint.y}px` }" @click.stop="insertCoverageVertex(midpoint.index)">+</button>
        <button v-for="point in overlay.coverageVertices" :key="`coverage-vertex-${point.index}`" type="button" class="earth-coverage-vertex" :aria-label="`区域边界节点 ${point.index + 1}，双击删除`" title="拖动调整范围，双击删除节点" :style="{ left: `${point.x}px`, top: `${point.y}px` }" @pointerdown="onCoverageVertexDown($event, point.index)" @pointermove="onCoverageVertexMove" @pointerup="onCoverageVertexEnd" @pointercancel="onCoverageVertexEnd" @lostpointercapture="onCoverageVertexEnd" @dblclick="deleteCoverageVertex($event, point.index)" @click.stop></button>
      </template>
      <div v-if="videoTrack?.length" class="earth-video-legend" aria-label="视频轨迹图例"><span><i class="track-key"></i>SRT 轨迹</span><span><i class="position-key">V</i>当前画面</span><span><i class="target-key">拍</i>近拍点</span></div>
      <span v-for="anchor in overlay.anchors" :key="anchor.id" class="earth-endpoint" :class="[anchor.role, { selected: anchor.selected }]" :style="{ left: `${anchor.x}px`, top: `${anchor.y}px` }">{{ anchor.label }}</span>
      <span v-for="target in overlay.captureTargets" :key="`video-target-${target.id}`" class="earth-capture-target" :title="`${target.label} · ${Math.floor(target.time_seconds / 60)}:${String(Math.floor(target.time_seconds % 60)).padStart(2, '0')}`" :aria-label="`${target.label} 拍摄位置`" :style="{ left: `${target.x}px`, top: `${target.y}px` }">{{ target.marker }}</span>
      <span v-if="overlay.weatherPoints.length" class="earth-weather-hint">点击编号查看该点天气</span>
      <button v-for="point in overlay.weatherPoints" :key="`weather-point-${point.index}`" type="button" class="earth-weather-marker" :class="[`is-${point.level}`, { selected: point.index === selectedWeatherPointIndex }]" :title="`查看航线点 ${point.index + 1} 天气`" :aria-label="`查看航线点 ${point.index + 1} 天气，${point.riskLabel}`" :aria-pressed="point.index === selectedWeatherPointIndex" :style="{ left: `${point.x}px`, top: `${point.y}px` }" @click.stop="selectedWeatherPointIndex = point.index">{{ point.index + 1 }}</button>
      <article v-if="selectedWeatherPoint" class="earth-weather-popover" :class="[`is-${selectedWeatherPoint.level}`, `is-${selectedWeatherPoint.placement}`]" :style="{ left: `${selectedWeatherPoint.popupX}px`, top: `${selectedWeatherPoint.popupY}px` }" @click.stop>
        <header><div><strong>航线点 {{ selectedWeatherPoint.index + 1 }}</strong><span>{{ selectedWeatherPoint.time }} · {{ selectedWeatherPoint.riskLabel }}</span></div><button type="button" aria-label="关闭点位天气" @click="selectedWeatherPointIndex = null">×</button></header>
        <small class="earth-weather-coordinate">坐标 {{ selectedWeatherPoint.coordinateLabel }}</small>
        <div class="earth-weather-values"><div><span>温度</span><b>{{ selectedWeatherPoint.temperature }}</b></div><div><span>高度风</span><b>{{ selectedWeatherPoint.heightWind }}</b></div><div><span>10 米风</span><b>{{ selectedWeatherPoint.groundWind }}</b></div><div><span>阵风</span><b>{{ selectedWeatherPoint.gust }}</b></div><div><span>能见度</span><b>{{ selectedWeatherPoint.visibility }}</b></div><div><span>6 小时雨</span><b>{{ selectedWeatherPoint.rain }}</b></div></div>
        <small v-if="selectedWeatherPoint.reasons" class="earth-weather-reason">{{ selectedWeatherPoint.reasons }}</small>
      </article>
      <div v-if="coverageDrawEnabled" class="earth-area-draw-surface" @pointerdown="onMapPointerDown" @pointermove="onMapPointerMove" @pointerup="onMapPointerUp" @pointercancel="onMapPointerUp"></div>
      <span v-for="node in overlay.autoNodes" :key="`auto-${node.index}`" class="earth-auto-node" aria-hidden="true" :style="{ left: `${node.x}px`, top: `${node.y}px` }"></span>
      <span v-for="node in overlay.groundNodes" :key="`ground-${node.index}`" class="earth-ground-node" aria-hidden="true" :style="{ left: `${node.x}px`, top: `${node.y}px` }"><span></span></span>
      <button v-for="node in overlay.nodes" :key="node.index" type="button" class="earth-node" :class="{ selected: node.index === selectedIndex }" :aria-label="`选择航点 ${node.index + 1}`" :aria-pressed="node.index === selectedIndex" :data-waypoint-index="node.index" :disabled="flightPose?.playing" :style="{ left: `${node.x}px`, top: `${node.y}px` }" @pointerdown="onNodeDown($event, node.index)" @pointermove="onNodeMove" @pointerup="onNodeUp" @pointercancel="onNodeUp" @lostpointercapture="onNodeUp" @click.stop="onNodeClick(node.index)"><span></span></button>
      <span v-if="overlay.videoPosition" class="earth-video-position" role="img" :aria-label="`视频当前位置，${overlay.videoPosition.time_seconds.toFixed(1)} 秒`" :title="`视频当前位置 · ${overlay.videoPosition.time_seconds.toFixed(1)} 秒${overlay.videoPosition.altitude_m !== null ? ` · ${overlay.videoPosition.altitude_m.toFixed(1)} m` : ''}`" :style="{ left: `${overlay.videoPosition.x}px`, top: `${overlay.videoPosition.y}px` }">V</span>
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
.earth-map.coverage-drawing .earth-map-canvas { cursor: crosshair; touch-action: none; }
.earth-area-draw-surface { position: absolute; z-index: 8; inset: 0; pointer-events: auto; touch-action: none; cursor: crosshair; }
.earth-map-overlay { z-index: 1; pointer-events: none; overflow: hidden; }
.earth-coverage-svg { position: absolute; z-index: 1; inset: 0; width: 100%; height: 100%; overflow: visible; pointer-events: none; }
.earth-coverage-svg polygon { fill: rgb(15 108 189 / .14); stroke: #0f6cbd; stroke-width: 2; stroke-dasharray: 7 4; vector-effect: non-scaling-stroke; }
.earth-coverage-vertex, .earth-coverage-edge-handle { position: absolute; z-index: 5; display: grid; transform: translate(-50%, -50%); place-items: center; padding: 0; border: 2px solid #fff; border-radius: 50%; box-shadow: 0 1px 5px rgb(15 23 42 / .45); color: #fff; pointer-events: auto; touch-action: none; cursor: grab; }
.earth-coverage-vertex { width: 18px; height: 18px; background: #0f6cbd; }
.earth-coverage-vertex:active { cursor: grabbing; }
.earth-coverage-vertex:hover:not(:disabled), .earth-coverage-edge-handle:hover:not(:disabled),
.earth-coverage-vertex:active:not(:disabled), .earth-coverage-edge-handle:active:not(:disabled) {
  /* Preserve the geographic anchor transform instead of the global button hover/press translation. */
  transform: translate(-50%, -50%);
  box-shadow: 0 1px 5px rgb(15 23 42 / .45);
}
.earth-coverage-vertex:focus-visible, .earth-coverage-edge-handle:focus-visible { outline: 2px solid #f5bd4f; outline-offset: 2px; }
.earth-coverage-edge-handle { width: 22px; height: 22px; border: 1px solid #0f6cbd; background: #fff; color: #0f6cbd; font-size: 16px; font-weight: 700; cursor: pointer; }
.earth-video-legend { position: absolute; z-index: 4; top: 12px; left: 12px; display: flex; flex-wrap: wrap; gap: 9px; padding: 6px 8px; border: 1px solid #d8e0e7; border-radius: 6px; background: rgb(255 255 255 / .94); color: #344455; font-size: 11px; }
.earth-video-legend span { display: inline-flex; align-items: center; gap: 4px; }
.earth-video-legend i { display: inline-grid; width: 14px; height: 14px; place-items: center; border-radius: 50%; color: #fff; font-size: 8px; font-style: normal; font-weight: 700; }
.earth-video-legend .track-key { width: 16px; height: 3px; border-radius: 0; background: #d97706; }
.earth-video-legend .position-key { background: #0f6cbd; }
.earth-video-legend .target-key { background: #d97706; }
.earth-weather-hint { position: absolute; z-index: 4; top: 12px; right: 12px; padding: 6px 9px; border: 1px solid #d8e0e7; border-radius: 6px; background: rgb(255 255 255 / .96); color: #526273; font-size: 11px; }
.earth-weather-marker { position: absolute; z-index: 6; display: grid; width: 26px; height: 26px; transform: translate(-50%, -50%); place-items: center; padding: 0; border: 2px solid #fff; border-radius: 50%; background: #0f6cbd; box-shadow: 0 1px 5px rgb(15 23 42 / .45); color: #fff; font-family: inherit; font-size: 10px; font-weight: 700; line-height: 1; pointer-events: auto; cursor: pointer; }
.earth-weather-marker:hover:not(:disabled), .earth-weather-marker:active:not(:disabled) { transform: translate(-50%, -50%); box-shadow: 0 1px 5px rgb(15 23 42 / .45); }
.earth-weather-marker.is-attention { background: #c77900; }
.earth-weather-marker.is-elevated { background: #bd302b; }
.earth-weather-marker.is-incomplete { background: #667788; }
.earth-weather-marker.selected { outline: 3px solid rgb(15 108 189 / .25); outline-offset: 2px; }
.earth-weather-popover { position: absolute; z-index: 6; display: grid; gap: 7px; box-sizing: border-box; width: min(258px, calc(100% - 16px)); transform: translate(-50%, -100%); padding: 10px; border: 1px solid #cbd5df; border-radius: 8px; background: #fff; box-shadow: 0 4px 14px rgb(15 23 42 / .2); color: #344455; font-size: 11px; line-height: 1.4; pointer-events: auto; }
.earth-weather-popover.is-below { transform: translate(-50%, 0); }
.earth-weather-popover.is-elevated { border-color: #c4312b; }
.earth-weather-popover.is-attention { border-color: #d68100; }
.earth-weather-popover.is-incomplete { border-color: #8795a1; }
.earth-weather-popover header { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; padding-bottom: 7px; border-bottom: 1px solid #e5e7eb; }
.earth-weather-popover header div { display: grid; gap: 2px; min-width: 0; }
.earth-weather-popover header strong { color: #173b5c; font-size: 13px; }
.earth-weather-popover header span { color: #64748b; font-size: 10px; font-variant-numeric: tabular-nums; }
.earth-weather-popover header button { display: grid; width: 24px; height: 24px; flex: 0 0 auto; place-items: center; padding: 0; border: 1px solid #d8e0e7; border-radius: 5px; background: #fff; color: #526273; font-family: inherit; font-size: 18px; line-height: 1; cursor: pointer; }
.earth-weather-coordinate { color: #64748b; font-size: 10px; font-variant-numeric: tabular-nums; }
.earth-weather-values { display: grid; grid-template-columns: 1fr 1fr; gap: 5px 10px; }
.earth-weather-values div { display: flex; justify-content: space-between; gap: 6px; min-width: 0; }
.earth-weather-values span { color: #64748b; white-space: nowrap; }
.earth-weather-values b { color: #26394c; font-weight: 600; text-align: right; white-space: nowrap; }
.earth-weather-reason { padding-top: 6px; border-top: 1px solid #eef0f2; color: #a4262c; font-size: 10px; line-height: 1.35; }
.earth-node { position: absolute; z-index: 5; width: 32px; height: 32px; transform: translate(-50%, -50%); display: grid; place-items: center; padding: 0; border: 0; border-radius: 50%; background: transparent; pointer-events: auto; cursor: pointer; touch-action: none; }
.earth-node:hover:not(:disabled), .earth-node:active:not(:disabled) { transform: translate(-50%, -50%); box-shadow: none; }
.earth-node span { width: 12px; height: 12px; border: 2px solid #fff; border-radius: 50%; background: #00e5a8; box-shadow: 0 1px 5px #12343b; }
.earth-node.selected span { width: 18px; height: 18px; background: #ffb547; }
.earth-node:focus-visible { outline: 2px solid #ffb547; }
.earth-video-position { position: absolute; z-index: 3; display: grid; width: 26px; height: 26px; transform: translate(-50%, -50%); place-items: center; border: 2px solid #fff; border-radius: 50%; background: #0f6cbd; box-shadow: 0 1px 5px rgb(18 52 59 / .45); color: #fff; font-size: 11px; font-weight: 700; pointer-events: none; }
.earth-capture-target { position: absolute; z-index: 2; display: grid; width: 24px; height: 24px; transform: translate(-50%, -50%); place-items: center; border: 2px solid #fff; border-radius: 50%; background: #d97706; box-shadow: 0 1px 5px rgb(18 52 59 / .4); color: #fff; font-size: 10px; font-weight: 700; pointer-events: none; }
.earth-ground-node { position: absolute; width: 14px; height: 14px; transform: translate(-50%, -50%); display: grid; place-items: center; pointer-events: none; }
.earth-ground-node span { width: 8px; height: 8px; border: 2px solid #fff; border-radius: 50%; background: #36dfba; box-shadow: 0 1px 4px #12343b; }
.earth-endpoint { position: absolute; transform: translate(-50%, -100%); padding: 3px 6px; border-radius: 4px; color: #fff; font-size: 12px; font-weight: 700; }
.earth-endpoint.start { background: #0f6cbd; }
.earth-endpoint.middle { background: #197b78; }
.earth-endpoint.end { background: #5b5fc7; }
.earth-endpoint.selected { outline: 2px solid #f5bd4f; outline-offset: 2px; z-index: 2; }
.earth-auto-node { position: absolute; width: 6px; height: 6px; transform: translate(-50%, -50%); border: 1px solid #fff; border-radius: 50%; background: #00aa83; box-shadow: 0 1px 3px rgb(18 52 59 / .5); pointer-events: none; }
.earth-flight-drone { position: absolute; z-index: 3; width: 40px; height: 40px; display: grid; place-items: center; border: 2px solid #fff; border-radius: 50%; background: #126cb7; box-shadow: 0 0 0 5px rgb(18 108 183 / .19), 0 4px 14px rgb(9 42 74 / .45); pointer-events: none; will-change: left, top, transform; }
.earth-flight-drone img { width: 28px; height: 28px; display: block; }
.earth-tilt-control { position: absolute; z-index: 2; right: 16px; top: 80px; display: flex; align-items: center; gap: 8px; min-height: 38px; padding: 0 12px; background: #fff; border: 1px solid #d8e0e7; border-radius: 8px; box-shadow: 0 4px 14px rgb(23 50 77 / .14); color: #344455; font-size: 12px; }
.earth-tilt-control input { width: 90px; }
.earth-map-status { position: absolute; z-index: 2; top: 50%; left: 50%; display: grid; justify-items: center; gap: 9px; max-width: min(320px, 80%); transform: translate(-50%, -50%); padding: 12px 16px; background: #fff; border: 1px solid #d8dde3; color: #424242; text-align: center; font-size: 13px; }
.earth-map-status button { padding: 5px 12px; border: 1px solid #0f6cbd; border-radius: 4px; background: #fff; color: #0f6cbd; cursor: pointer; }
.earth-attribution { position: absolute; bottom: 4px; right: 6px; color: #fff; text-shadow: 0 1px 2px #000; font-size: 11px; }
@media (max-width: 900px) { .earth-tilt-control { top: 164px; right: 10px; } }
@media (max-width: 600px) { .earth-weather-popover { width: min(242px, calc(100% - 16px)); } .earth-weather-hint { top: auto; right: 8px; bottom: 12px; font-size: 10px; } }
</style>

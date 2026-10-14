<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import WeatherPanels from './WeatherPanels.vue'
import RouteWeatherPanel from './RouteWeatherPanel.vue'
import WeatherWindowsPanel from './WeatherWindowsPanel.vue'
import RoadWeatherComparison from './RoadWeatherComparison.vue'
import EarthMap from './EarthMap.vue'
import PlaceSearch from './PlaceSearch.vue'
import BaiduCameraPreview from './BaiduCameraPreview.vue'
import type { CameraPose } from './baiduScene'
import type { RouteAssessment, WeatherLimits } from './weatherTypes'
import { weatherOnlyChecks } from './weatherPresentation'
import type { RoadPreview as Preview } from './routeTypes'

type Point = { latitude: number; longitude: number }
const workspacePanel = ref<'settings' | 'map' | 'overview'>('overview')
const settingsPage = ref<'points' | 'mission'>('points')
type LandingCheck = {
  state: 'mapped_building' | 'road_nearby' | 'no_road'
  search_radius_m: number
  road_distance_m: number | null
  road_point: number[] | null
  message: string
  landing_verified: false
}

const form = reactive({
  // Approximate demonstration points at Changzhou Vocational University of Information Technology.
  start: { latitude: 31.67920, longitude: 119.95150 },
  end: { latitude: 31.68040, longitude: 119.94950 },
  altitude_m: 100,
  speed_kmh: 45,
  clearance_m: 2,
})
const preview = ref<Preview | null>(null)
const previewKey = ref('')
const weatherJobId = ref<string | null>(null)
const weatherState = ref({ status: 'idle', message: '等待天气预测。' })
const routeAssessment = ref<RouteAssessment | null>(null)
const routePhase = ref({ pending: false, error: '' })
const windowPhase = ref('waiting')
const comparisonPhase = ref('waiting')
const customLimitsEnabled = ref(false)
const customLimits = reactive<WeatherLimits>({ name: '自定义阈值', max_wind_m_s: 12, max_crosswind_m_s: 8,
  max_headwind_m_s: 12, max_gust_m_s: 15, max_rain_6h_mm: 5, min_visibility_m: 3000 })
const activeLimits = computed(() => customLimitsEnabled.value ? customLimits : null)
function currentDepartureBeijing() {
  // Datetime-local only keeps minutes; use the next minute so the value stays
  // valid while the weather requests are being prepared.
  return new Date(Date.now() + 60 * 1000 + 8 * 3600 * 1000).toISOString().slice(0, 16)
}
const latestDepartureBeijing = ref(currentDepartureBeijing())
const departureBeijing = ref(latestDepartureBeijing.value)
const departureAutoManaged = ref(true)
let departureClockTimer: ReturnType<typeof setTimeout> | null = null
const loading = ref(false)
const kmzLoading = ref(false)
const kmzError = ref('')
const djiModel = ref<'fly' | 'm30' | 'm3e'>('fly')
type WaypointHeadingMode = 'followWayline' | 'manually' | 'fixed' | 'smoothTransition' | 'towardPOI'
type WaypointSetting = {
  altitude_m: number
  heading_mode: WaypointHeadingMode
  heading_angle: number
  gimbal_pitch_angle: number
  export_anchor?: boolean
}
const waypointSettings = ref<WaypointSetting[]>([])
const checkingPoint = ref(false)
const error = ref('')
const pointHint = ref('')
const mapMode = ref<'start' | 'end'>('start')
const baseMap = ref<'current' | 'earth'>('current')
const earthMapMounted = ref(false)
const earthMapRef = ref<InstanceType<typeof EarthMap> | null>(null)
const mapViewMode = ref<'2d' | '3d'>('3d')
const mapEditMode = ref<'route' | 'node'>('route')
const selectedWaypointIndex = ref<number | null>(null)
const draggingCameraIndex = ref<number | null>(null)
let mapCameraInputsBeforeDrag: boolean | null = null
const suppressNextMapClick = ref(false)
const roadSource = ref<'local' | 'online'>('local')
const mapReady = ref(false)
const mapElement = ref<HTMLElement | null>(null)
const cameraPreviewElement = ref<HTMLElement | null>(null)
const cesiumCameraElement = ref<HTMLElement | null>(null)
const cameraPreviewReady = ref(false)
let cameraPreviewViewer: any = null
let waypointRenderFrame: number | null = null
let waypointRouteDirty = false
let selectedViewFrustum: ReturnType<typeof cameraFrustum> | null = null
const landingSearchRadiusM = ref(35)
const showStartRange = ref(false)
const showEndRange = ref(false)
type ResizeSide = 'left' | 'right'
const sidebarWidth = ref(328)
const insightsWidth = ref(320)
const resizingSide = ref<ResizeSide | null>(null)
let resizeStartX = 0
let resizeStartWidth = 0
let viewer: any = null
let Cesium: any = null
let routeController: AbortController | null = null
let routeRequestVersion = 0

const elapsed = computed(() => preview.value && form.speed_kmh > 0 ? `${(preview.value.distance_km / form.speed_kmh * 60).toFixed(2)} 分钟` : '待计算')
const distance = computed(() => preview.value ? `${preview.value.distance_km.toFixed(2)} km` : '待计算')
const layoutStyle = computed(() => ({ '--sidebar-width': `${sidebarWidth.value}px`, '--insights-width': `${insightsWidth.value}px` }))
const routeKey = computed(() => JSON.stringify([form.start, form.end, roadSource.value, form.clearance_m]))
const weatherCoordinates = computed(() => preview.value && previewKey.value === routeKey.value ? preview.value.geometry.coordinates : null)
const earthRoute = computed(() => preview.value?.geometry.coordinates.map(([longitude, latitude], index) => [longitude, latitude, waypointSettings.value[index]?.altitude_m ?? form.altitude_m]) ?? null)
const selectedCameraPose = computed<CameraPose | null>(() => {
  const index = selectedWaypointIndex.value
  const coordinate = index === null ? null : preview.value?.geometry.coordinates[index]
  if (index === null || !coordinate) return null
  return { longitude: coordinate[0], latitude: coordinate[1], altitude: waypointSettings.value[index]?.altitude_m ?? form.altitude_m,
    heading: waypointHeading(index), pitch: waypointSettings.value[index]?.gimbal_pitch_angle ?? 0 }
})
const earthFootprint = computed(() => {
  const index = selectedWaypointIndex.value
  if (!Cesium || !preview.value || index === null || mapEditMode.value !== 'node') return []
  return cameraFrustum(index, Math.max(10, Number(form.altitude_m) || 100)).ground.map((position: any) => {
    const point = Cesium.Cartographic.fromCartesian(position)
    return [Cesium.Math.toDegrees(point.longitude), Cesium.Math.toDegrees(point.latitude), point.height]
  })
})
const decisionTone = computed(() => {
  if (routeAssessment.value) return `risk-${routeAssessment.value.weather_risk.level}`
  return routePhase.value.error || ['blocked', 'failed'].includes(weatherState.value.status) ? 'risk-incomplete' : 'pending'
})
const decisionTitle = computed(() => {
  if (!weatherCoordinates.value) return '先计算道路路线'
  if (['blocked', 'failed'].includes(weatherState.value.status)) return '天气预测不可用'
  if (routePhase.value.error) return '沿线天气检查未完成'
  if (routePhase.value.pending) return '正在检查沿线天气'
  if (!routeAssessment.value) return weatherJobId.value ? '等待沿线天气结果' : '正在准备天气资料'
  return ({
    no_trigger: '当前规则下没有明显天气提醒',
    attention: '有天气因素需要留意',
    elevated: '发现较强天气提醒',
    incomplete: '天气资料不完整，暂不能判断',
  } as Record<string, string>)[routeAssessment.value.weather_risk.level] || '天气初筛已完成'
})
const decisionDescription = computed(() => {
  if (!weatherCoordinates.value) return '先在左侧完成起点、终点和飞行参数，系统才能沿路线匹配天气。'
  if (['blocked', 'failed'].includes(weatherState.value.status)) return weatherState.value.message
  if (routePhase.value.error) return routePhase.value.error
  if (routePhase.value.pending) return '系统正在按出发时间、飞行高度和路线逐点检查天气。'
  if (!routeAssessment.value) return '结果出来后，这里会先告诉你是否有需要留意的天气因素。'
  if (routeAssessment.value.weather_risk.level === 'no_trigger') return '已覆盖的风、降雨、阵风和能见度没有触发当前规则。仍需结合机型和现场情况核验。'
  if (routeAssessment.value.weather_risk.level === 'incomplete') return '部分路线缺少可用的高度或时间资料，不能把“没有提醒”理解为安全。'
  return '请展开下方的沿线天气检查，查看触发因素和出现位置。这个结果只是天气初筛。'
})
const weatherChecks = computed(() => routeAssessment.value ? weatherOnlyChecks(routeAssessment.value.decision) : [])
const alignmentProgress = computed(() => routeAssessment.value?.coverage.flight_height ? '沿线位置、时间与高度风已匹配'
  : routeAssessment.value?.coverage.surface_space_time ? '地面时空已匹配，高度资料不足' : '等待路线与天气资料匹配')
const planningProgress = computed(() => {
  if (windowPhase.value === 'pending' || comparisonPhase.value === 'pending') return '正在比较候选时次与道路路线'
  if (windowPhase.value === 'no_covered_candidate' || comparisonPhase.value === 'coverage_incomplete') return '候选天气覆盖不足，暂不能完成比较'
  if (windowPhase.value === 'available' && comparisonPhase.value === 'no_resolvable_weather_difference') return '时次已比较；无可分辨的天气绕行收益'
  if (windowPhase.value === 'available' && comparisonPhase.value === 'candidate_lower_screening') return '时次已比较；存在较低初筛分道路备选'
  if (windowPhase.value === 'available' && ['no_alternative', 'no_material_improvement'].includes(comparisonPhase.value)) return '时次与道路候选已比较；保留道路基线'
  if (windowPhase.value === 'failed' || comparisonPhase.value === 'failed') return '部分比较未完成，请查看接口提示'
  return '候选时次与路线等待当前资料'
})

watch(routeKey, () => {
  if (preview.value && previewKey.value !== routeKey.value) { preview.value = null; routeAssessment.value = null; drawCesiumRoute(false) }
})
watch(routeAssessment, () => { drawCesiumRoute(false) })
watch(workspacePanel, async () => {
  await nextTick()
  if (workspacePanel.value === 'map') viewer?.resize()
})

async function chooseBaseMap(next: 'current' | 'earth') {
  if (baseMap.value === next) return
  cancelCameraPreviewDrag()
  finishMapCameraDrag()
  if (next === 'earth') earthMapMounted.value = true
  baseMap.value = next
  await nextTick()
  if (viewer) viewer.useDefaultRenderLoop = next === 'current'
  if (cameraPreviewViewer) cameraPreviewViewer.useDefaultRenderLoop = next === 'current' && mapEditMode.value === 'node' && selectedWaypointIndex.value !== null
  if (next === 'current') {
    viewer?.resize()
    applyCurrentMapView()
    updateCameraPreview()
  }
}

function applyCurrentMapView() {
  if (!viewer?.scene) return
  if (mapViewMode.value === '2d') viewer.scene.morphTo2D(0)
  else viewer.scene.morphTo3D(0)
  drawCesiumRoute(true)
  viewer.scene.requestRender()
}

function chooseMapViewMode(next: '2d' | '3d') {
  if (mapViewMode.value === next) return
  cancelCameraPreviewDrag()
  finishMapCameraDrag()
  mapViewMode.value = next
  if (baseMap.value === 'current') applyCurrentMapView()
}

watch([mapEditMode, selectedWaypointIndex], () => {
  cancelCameraPreviewDrag()
  finishMapCameraDrag()
}, { flush: 'sync' })

function revealInvalidSetting(event: Event) {
  const control = event.target as HTMLInputElement
  const group = control.closest<HTMLElement>('[data-settings-page]')
  if (!group || control.getClientRects().length) return
  event.preventDefault()
  settingsPage.value = group.dataset.settingsPage === 'mission' ? 'mission' : 'points'
  void nextTick(() => { control.focus({ preventScroll: true }) })
}

function formatted(point: Point) {
  return `${point.latitude.toFixed(4)}°N, ${point.longitude.toFixed(4)}°E`
}

function refreshDepartureClock() {
  const latest = currentDepartureBeijing()
  latestDepartureBeijing.value = latest
  if (departureAutoManaged.value && departureBeijing.value < latest) departureBeijing.value = latest
}
function scheduleDepartureClock() {
  refreshDepartureClock()
  const delay = 60_000 - (Date.now() % 60_000)
  departureClockTimer = setTimeout(scheduleDepartureClock, delay)
}
function markDepartureManual() {
  departureAutoManaged.value = false
}
function chooseDeparture(time: string) {
  departureAutoManaged.value = false
  departureBeijing.value = new Date(new Date(time).getTime() + 8 * 3600 * 1000).toISOString().slice(0, 16)
}

function syncWaypointSettings(route: Preview) {
  const previous = new Map<string, WaypointSetting>()
  preview.value?.geometry.coordinates.forEach((point, index) => {
    const setting = waypointSettings.value[index]
    if (setting) previous.set(`${point[0]},${point[1]}`, setting)
  })
  waypointSettings.value = route.geometry.coordinates.map(([longitude, latitude]) => previous.get(`${longitude},${latitude}`) ?? ({
    altitude_m: form.altitude_m,
    heading_mode: 'followWayline',
    heading_angle: 0,
    gimbal_pitch_angle: 0,
  }))
}

function selectedWaypointSetting() {
  const index = selectedWaypointIndex.value
  return index === null ? null : waypointSettings.value[index] ?? null
}

function waypointHeading(index: number) {
  const setting = waypointSettings.value[index]
  if (setting && setting.heading_mode !== 'followWayline' && setting.heading_mode !== 'manually') return setting.heading_angle
  const coordinates = preview.value?.geometry.coordinates ?? []
  const current = coordinates[index]
  const target = coordinates[index + 1] ?? coordinates[index - 1]
  if (!current || !target) return 0
  const lat1 = current[1] * Math.PI / 180
  const lat2 = target[1] * Math.PI / 180
  const dLon = (target[0] - current[0]) * Math.PI / 180
  const bearing = Math.atan2(Math.sin(dLon) * Math.cos(lat2), Math.cos(lat1) * Math.sin(lat2) - Math.sin(lat1) * Math.cos(lat2) * Math.cos(dLon)) * 180 / Math.PI
  return (bearing + 360) % 360
}

function offsetCoordinate(longitude: number, latitude: number, bearing: number, distanceM: number): [number, number] {
  const radius = 6371008.8
  const angularDistance = distanceM / radius
  const bearingRad = bearing * Math.PI / 180
  const lat1 = latitude * Math.PI / 180
  const lon1 = longitude * Math.PI / 180
  const lat2 = Math.asin(Math.sin(lat1) * Math.cos(angularDistance) + Math.cos(lat1) * Math.sin(angularDistance) * Math.cos(bearingRad))
  const lon2 = lon1 + Math.atan2(Math.sin(bearingRad) * Math.sin(angularDistance) * Math.cos(lat1), Math.cos(angularDistance) - Math.sin(lat1) * Math.sin(lat2))
  return [lon2 * 180 / Math.PI, lat2 * 180 / Math.PI]
}

type LocalVector = { east: number; north: number; up: number }

function cameraFrustum(index: number, routeAltitude: number) {
  const coordinates = preview.value?.geometry.coordinates ?? []
  const [longitude, latitude] = coordinates[index] ?? [0, 0]
  const setting = waypointSettings.value[index]
  const altitude = setting?.altitude_m ?? routeAltitude
  const yaw = waypointHeading(index) * Math.PI / 180
  const pitch = (setting?.gimbal_pitch_angle ?? 0) * Math.PI / 180
  const halfHorizontal = 78 * Math.PI / 360
  const halfVertical = 58 * Math.PI / 360
  const forward: LocalVector = { east: Math.sin(yaw) * Math.cos(pitch), north: Math.cos(yaw) * Math.cos(pitch), up: Math.sin(pitch) }
  const right: LocalVector = { east: Math.cos(yaw), north: -Math.sin(yaw), up: 0 }
  const top: LocalVector = { east: -Math.sin(yaw) * Math.sin(pitch), north: -Math.cos(yaw) * Math.sin(pitch), up: Math.cos(pitch) }
  const normalize = (vector: LocalVector): LocalVector => {
    const length = Math.hypot(vector.east, vector.north, vector.up) || 1
    return { east: vector.east / length, north: vector.north / length, up: vector.up / length }
  }
  const direction = (horizontal: number, vertical: number) => normalize({
    east: forward.east + right.east * horizontal * Math.tan(halfHorizontal) + top.east * vertical * Math.tan(halfVertical),
    north: forward.north + right.north * horizontal * Math.tan(halfHorizontal) + top.north * vertical * Math.tan(halfVertical),
    up: forward.up + right.up * horizontal * Math.tan(halfHorizontal) + top.up * vertical * Math.tan(halfVertical),
  })
  const corners = [direction(-1, 1), direction(1, 1), direction(1, -1), direction(-1, -1)]
  const toCartesian = (east: number, north: number, up: number) => {
    const distance = Math.hypot(east, north)
    const bearing = Math.atan2(east, north) * 180 / Math.PI
    const point = offsetCoordinate(longitude, latitude, bearing, distance)
    return Cesium.Cartesian3.fromDegrees(point[0], point[1], Math.max(0, altitude + up))
  }
  const atDistance = (vector: LocalVector, distance: number) => toCartesian(vector.east * distance, vector.north * distance, vector.up * distance)
  const groundEntries = corners.map((vector) => {
    const distance = vector.up < -0.08 ? Math.min(420, Math.max(40, altitude / -vector.up)) : 260
    return { east: vector.east * distance, north: vector.north * distance, position: atDistance(vector, distance) }
  })
  // Keep the camera's perimeter order. Sorting by the ground projection swaps
  // upper/lower corners when the camera is horizontal and folds the 3D faces.
  const ground = groundEntries.map((entry) => entry.position)
  const groundCenter = atDistance(forward, forward.up < -0.08 ? Math.min(420, Math.max(40, altitude / -forward.up)) : 260)
  const origin = Cesium.Cartesian3.fromDegrees(longitude, latitude, altitude)
  const faces = ground.map((corner, faceIndex) => [origin, corner, ground[(faceIndex + 1) % ground.length]])
  return { origin, ground, groundCenter, faces, heading: yaw * 180 / Math.PI, pitch: pitch * 180 / Math.PI }
}

function adjustSelectedWaypoint(change: Partial<WaypointSetting>, immediate = false) {
  const index = selectedWaypointIndex.value
  if (index === null || !waypointSettings.value[index]) return
  Object.assign(waypointSettings.value[index], change, { export_anchor: true })
  if (immediate) {
    if (change.altitude_m !== undefined) drawCesiumRoute(false)
    else updateSelectedWaypointView()
    updateCameraPreview()
    return
  }
  scheduleWaypointRender(change.altitude_m !== undefined)
}

function setSelectedWaypointOrientation(heading: number, pitch?: number, immediate = false) {
  const change: Partial<WaypointSetting> = {
    heading_mode: 'smoothTransition',
    heading_angle: ((heading % 360) + 360) % 360,
  }
  if (pitch !== undefined) change.gimbal_pitch_angle = Math.max(-90, Math.min(45, pitch))
  adjustSelectedWaypoint(change, immediate)
}

function rotateSelectedWaypoint(headingDelta = 0, pitchDelta = 0) {
  const index = selectedWaypointIndex.value
  const setting = selectedWaypointSetting()
  if (index === null || !setting) return
  if (headingDelta) setSelectedWaypointOrientation(waypointHeading(index) + headingDelta)
  if (pitchDelta) adjustSelectedWaypoint({ gimbal_pitch_angle: Math.max(-90, Math.min(45, setting.gimbal_pitch_angle + pitchDelta)) })
}

function updateSelectedWaypointView() {
  const index = selectedWaypointIndex.value
  if (!viewer || !Cesium || !preview.value || index === null || mapEditMode.value !== 'node') return
  const arrow = viewer.entities.getById(`waypoint-direction-${index}`)
  if (!arrow) return
  selectedViewFrustum = cameraFrustum(index, Math.max(10, Number(form.altitude_m) || 100))
  arrow.billboard.rotation = Cesium.Math.toRadians(selectedViewFrustum.heading - 90)
  viewer.scene.requestRender()
}

function scheduleWaypointRender(redrawRoute = true) {
  waypointRouteDirty ||= redrawRoute
  if (waypointRenderFrame !== null) return
  waypointRenderFrame = requestAnimationFrame(() => {
    waypointRenderFrame = null
    if (waypointRouteDirty) drawCesiumRoute(false)
    else updateSelectedWaypointView()
    waypointRouteDirty = false
    updateCameraPreview()
  })
}

function moveSelectedWaypoint(eastMeters: number, northMeters: number) {
  const index = selectedWaypointIndex.value
  if (index === null || !preview.value?.geometry.coordinates[index]) return
  const coordinates = preview.value.geometry.coordinates
  const [longitude, latitude] = coordinates[index]
  const setting = waypointSettings.value[index]
  if (setting?.heading_mode === 'followWayline') {
    const heading = waypointHeading(index)
    setting.heading_mode = 'smoothTransition'
    setting.heading_angle = heading
  }
  const distance = Math.hypot(eastMeters, northMeters)
  if (!distance) return
  const bearing = Math.atan2(eastMeters, northMeters) * 180 / Math.PI
  coordinates[index] = offsetCoordinate(longitude, latitude, bearing, distance)
  if (setting) setting.export_anchor = true
  scheduleWaypointRender()
}

function markWaypointEdited(event: Event) {
  const row = (event.target as Element).closest('.waypoint-row')
  if (!row) return
  const index = Array.from(row.parentElement?.querySelectorAll('.waypoint-row') ?? []).indexOf(row)
  if (index >= 0 && waypointSettings.value[index]) {
    waypointSettings.value[index].export_anchor = true
    scheduleWaypointRender()
  }
}

function selectWaypoint(index: number) {
  if (!preview.value || index < 0 || index >= waypointSettings.value.length) return
  if (selectedWaypointIndex.value === index && mapEditMode.value === 'node') return
  selectedWaypointIndex.value = index
  mapEditMode.value = 'node'
  drawCesiumRoute(false)
  focusSelectedWaypoint()
}

function focusSelectedWaypoint() {
  if (!preview.value || selectedWaypointIndex.value === null) return
  const index = selectedWaypointIndex.value
  if (baseMap.value === 'earth') {
    earthMapRef.value?.focusWaypoint(index)
    return
  }
  if (!viewer || !Cesium) return
  const [longitude, latitude] = preview.value.geometry.coordinates[index]
  const setting = waypointSettings.value[index]
  const altitude = setting?.altitude_m ?? form.altitude_m
  const heading = waypointHeading(index)
  if (mapViewMode.value === '2d') {
    viewer.camera.setView({ destination: Cesium.Cartesian3.fromDegrees(longitude, latitude, 700),
      orientation: { heading: 0, pitch: Cesium.Math.toRadians(-90), roll: 0 } })
    viewer.scene.requestRender()
    updateCameraPreview()
    return
  }
  const rear = offsetCoordinate(longitude, latitude, (heading + 180) % 360, 220)
  viewer.camera.flyTo({
    destination: Cesium.Cartesian3.fromDegrees(rear[0], rear[1], altitude + 150),
    orientation: { heading: Cesium.Math.toRadians(heading), pitch: Cesium.Math.toRadians(-26), roll: 0 },
    duration: 0.65,
  })
  updateCameraPreview()
}

function updateCameraPreview() {
  if (baseMap.value === 'earth') return
  if (!preview.value || selectedWaypointIndex.value === null || mapEditMode.value !== 'node' || !Cesium) return
  if (!cameraPreviewViewer) initCameraPreview()
  if (!cameraPreviewViewer) return
  const index = selectedWaypointIndex.value
  const [longitude, latitude] = preview.value.geometry.coordinates[index]
  const setting = waypointSettings.value[index]
  const altitude = setting?.altitude_m ?? form.altitude_m
  const heading = waypointHeading(index)
  const pitch = setting?.gimbal_pitch_angle ?? 0
  cameraPreviewViewer.camera.setView({
    destination: Cesium.Cartesian3.fromDegrees(longitude, latitude, altitude),
    orientation: { heading: Cesium.Math.toRadians(heading), pitch: Cesium.Math.toRadians(pitch), roll: 0 },
  })
  cameraPreviewViewer.camera.frustum.near = 0.5
  cameraPreviewViewer.camera.frustum.far = 5000
  cameraPreviewViewer.scene.requestRender()
}

let cameraPreviewDrag: { pointerId: number; waypointIndex: number; x: number; y: number; heading: number; headingMode: WaypointHeadingMode; headingAngle: number; pitch: number; width: number; height: number; axis: 'pitch' | 'both' | null } | null = null

function handleCameraPreviewPointerDown(event: PointerEvent) {
  const index = selectedWaypointIndex.value
  const setting = selectedWaypointSetting()
  const element = cameraPreviewElement.value
  if (event.button !== 0 || !event.isPrimary || cameraPreviewDrag || mapEditMode.value !== 'node' || index === null || !setting || !element) return
  event.preventDefault()
  const bounds = element.getBoundingClientRect()
  cameraPreviewDrag = {
    pointerId: event.pointerId,
    waypointIndex: index,
    x: event.clientX,
    y: event.clientY,
    heading: waypointHeading(index),
    headingMode: setting.heading_mode,
    headingAngle: setting.heading_angle,
    pitch: setting.gimbal_pitch_angle ?? 0,
    width: Math.max(1, bounds.width),
    height: Math.max(1, bounds.height),
    axis: null,
  }
  element.setPointerCapture(event.pointerId)
}

function handleCameraPreviewPointerMove(event: PointerEvent) {
  const drag = cameraPreviewDrag
  if (!drag || drag.pointerId !== event.pointerId) return
  if (mapEditMode.value !== 'node' || selectedWaypointIndex.value !== drag.waypointIndex) {
    cancelCameraPreviewDrag()
    return
  }
  const deltaX = event.clientX - drag.x
  const deltaY = event.clientY - drag.y
  if (!drag.axis && Math.hypot(deltaX, deltaY) >= 8) drag.axis = Math.abs(deltaX) > Math.abs(deltaY) ? 'both' : 'pitch'
  if (!drag.axis) return
  const heading = drag.heading + deltaX / drag.width * 180
  const pitch = drag.pitch - deltaY / drag.height * 90
  if (drag.axis === 'pitch') {
    adjustSelectedWaypoint({
      heading_mode: drag.headingMode,
      heading_angle: drag.headingAngle,
      gimbal_pitch_angle: Math.max(-90, Math.min(45, pitch)),
    }, true)
  } else {
    setSelectedWaypointOrientation(heading, pitch, true)
  }
}

function handleCameraPreviewPointerEnd(event: PointerEvent) {
  if (cameraPreviewDrag?.pointerId !== event.pointerId) return
  cancelCameraPreviewDrag()
}

function cancelCameraPreviewDrag() {
  const drag = cameraPreviewDrag
  cameraPreviewDrag = null
  const element = cameraPreviewElement.value
  if (drag && element?.hasPointerCapture(drag.pointerId)) element.releasePointerCapture(drag.pointerId)
}

function finishMapCameraDrag() {
  if (draggingCameraIndex.value === null) return
  draggingCameraIndex.value = null
  suppressNextMapClick.value = true
  if (viewer && mapCameraInputsBeforeDrag !== null) viewer.scene.screenSpaceCameraController.enableInputs = mapCameraInputsBeforeDrag
  mapCameraInputsBeforeDrag = null
}

watch([mapEditMode, selectedWaypointIndex, baseMap], async () => {
  await nextTick()
  if (mapEditMode.value === 'node' && selectedWaypointIndex.value !== null) {
    updateCameraPreview()
    if (cameraPreviewViewer && baseMap.value === 'current') {
      cameraPreviewViewer.useDefaultRenderLoop = true
      cameraPreviewViewer.resize()
      cameraPreviewViewer.scene.requestRender()
    }
  }
  if (cameraPreviewViewer && (baseMap.value === 'earth' || mapEditMode.value !== 'node' || selectedWaypointIndex.value === null)) {
    cameraPreviewViewer.useDefaultRenderLoop = false
  }
})

watch(mapEditMode, () => { drawCesiumRoute(false) })

function handleWaypointKeydown(event: KeyboardEvent) {
  const target = event.target as HTMLElement | null
  if (target?.matches('input, select, textarea, [contenteditable="true"]')) return
  if (mapEditMode.value !== 'node' || selectedWaypointIndex.value === null) return
  const key = event.key.toLowerCase()
  if (key === 'w' || key === 's') {
    event.preventDefault()
    moveSelectedWaypoint(0, key === 'w' ? 1 : -1)
  } else if (key === 'a' || key === 'd') {
    event.preventDefault()
    moveSelectedWaypoint(key === 'a' ? -1 : 1, 0)
  } else if (key === 'q' || key === 'e' || event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
    event.preventDefault()
    const delta = key === 'q' || event.key === 'ArrowLeft' ? -5 : 5
    rotateSelectedWaypoint(delta)
  } else if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
    event.preventDefault()
    rotateSelectedWaypoint(0, event.key === 'ArrowUp' ? 5 : -5)
  } else if (key === 'z' || key === 'c') {
    event.preventDefault()
    rotateSelectedWaypoint(0, key === 'z' ? 5 : -5)
  }
}

function chooseRoad(route: Preview) {
  cancelCalculation()
  syncWaypointSettings(route)
  preview.value = route
  previewKey.value = routeKey.value
  routeAssessment.value = null
  drawCesiumRoute()
}

async function exportDjiKmz() {
  if (!preview.value || kmzLoading.value) return
  kmzLoading.value = true
  kmzError.value = ''
  try {
    const response = await fetch('/api/routes/export-kmz', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ coordinates: preview.value.geometry.coordinates, altitude_m: form.altitude_m, speed_kmh: form.speed_kmh, drone_model: djiModel.value, waypoint_settings: waypointSettings.value }),
    })
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}))
      throw new Error(typeof payload.detail === 'string' ? payload.detail : 'DJI KMZ 导出失败。')
    }
    const blob = await response.blob()
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = 'dji_waypoint_draft.kmz'
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(url)
  } catch (cause) {
    kmzError.value = cause instanceof Error ? cause.message : 'DJI KMZ 导出失败。'
  } finally {
    kmzLoading.value = false
  }
}

async function calculate(flyToRoute: boolean | SubmitEvent = true) {
  if (loading.value) return
  const controller = new AbortController()
  const requestVersion = ++routeRequestVersion
  const requestedKey = routeKey.value
  routeController = controller
  loading.value = true
  error.value = ''
  kmzError.value = ''
  pointHint.value = ''
  preview.value = null
  drawCesiumRoute()
  try {
    const response = await fetch('/api/routes/plan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...form, road_source: roadSource.value }),
      signal: controller.signal,
    })
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}))
      throw new Error(typeof payload.detail === 'string' ? payload.detail : '道路路线接口暂时不可用。')
    }
    const result: Preview = await response.json()
    if (controller.signal.aborted || requestVersion !== routeRequestVersion) return
    syncWaypointSettings(result)
    preview.value = result
    previewKey.value = requestedKey
    drawCesiumRoute(typeof flyToRoute === 'boolean' ? flyToRoute : true)
  } catch (cause) {
    if (controller.signal.aborted || requestVersion !== routeRequestVersion) return
    error.value = cause instanceof Error ? cause.message : '无法生成航线预览。'
  } finally {
    if (requestVersion === routeRequestVersion) {
      routeController = null
      loading.value = false
    }
  }
}

function cancelCalculation() {
  if (!routeController) return
  routeRequestVersion += 1
  routeController.abort()
  routeController = null
  loading.value = false
}

function chooseRoadSource(source: 'local' | 'online') {
  if (loading.value || checkingPoint.value || roadSource.value === source) return
  roadSource.value = source
  calculate()
}

function clamp(value: number, minimum: number, maximum: number) {
  return Math.min(Math.max(value, minimum), maximum)
}
function minimumWidth(side: ResizeSide) {
  return side === 'left' ? 260 : 280
}
function widthLimit(side: ResizeSide) {
  const mapMinimum = 420
  const other = side === 'left' ? insightsWidth.value : sidebarWidth.value
  return Math.min(480, Math.max(minimumWidth(side), window.innerWidth - other - mapMinimum))
}
function resizeViewer() {
  requestAnimationFrame(() => { if (viewer && !viewer.isDestroyed()) viewer.resize() })
}
function setColumnWidth(side: ResizeSide, value: number) {
  if (side === 'left') sidebarWidth.value = clamp(value, minimumWidth('left'), widthLimit('left'))
  else insightsWidth.value = clamp(value, minimumWidth('right'), widthLimit('right'))
  resizeViewer()
}
function onColumnResize(event: PointerEvent) {
  if (!resizingSide.value) return
  const delta = event.clientX - resizeStartX
  setColumnWidth(resizingSide.value, resizeStartWidth + (resizingSide.value === 'left' ? delta : -delta))
}
function stopColumnResize() {
  if (!resizingSide.value) return
  window.removeEventListener('pointermove', onColumnResize)
  window.removeEventListener('pointerup', stopColumnResize)
  window.removeEventListener('pointercancel', stopColumnResize)
  document.body.classList.remove('is-resizing-columns')
  resizingSide.value = null
}
function startColumnResize(side: ResizeSide, event: PointerEvent) {
  if (window.innerWidth <= 1100) return
  event.preventDefault()
  const handle = event.currentTarget as HTMLElement | null
  handle?.setPointerCapture?.(event.pointerId)
  resizingSide.value = side
  resizeStartX = event.clientX
  resizeStartWidth = side === 'left' ? sidebarWidth.value : insightsWidth.value
  document.body.classList.add('is-resizing-columns')
  window.addEventListener('pointermove', onColumnResize)
  window.addEventListener('pointerup', stopColumnResize)
  window.addEventListener('pointercancel', stopColumnResize)
}
function resizeColumnByKeyboard(side: ResizeSide, event: KeyboardEvent) {
  const step = event.shiftKey ? 48 : 16
  const current = side === 'left' ? sidebarWidth.value : insightsWidth.value
  if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
    event.preventDefault()
    const direction = side === 'left' ? (event.key === 'ArrowRight' ? 1 : -1) : (event.key === 'ArrowLeft' ? 1 : -1)
    setColumnWidth(side, current + direction * step)
  } else if (event.key === 'Home' || event.key === 'End') {
    event.preventDefault()
    setColumnWidth(side, event.key === 'Home' ? minimumWidth(side) : widthLimit(side))
  }
}
function resetColumnWidths() {
  sidebarWidth.value = 328
  insightsWidth.value = 320
  resizeViewer()
}
function toggleRange(point: 'start' | 'end') {
  if (point === 'start') showStartRange.value = !showStartRange.value
  else showEndRange.value = !showEndRange.value
  drawCesiumRoute(false)
}

async function chooseMapPoint(point: Point) {
  if (loading.value || checkingPoint.value) return
  const mode = mapMode.value
  checkingPoint.value = true
  error.value = ''
  pointHint.value = ''
  try {
    const response = await fetch('/api/landing/check', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ point, clearance_m: form.clearance_m }),
    })
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}))
      throw new Error(typeof payload.detail === 'string' ? payload.detail : '暂时无法检查该点位。')
    }
    const inspection: LandingCheck = await response.json()
    if (inspection.state === 'mapped_building') {
      error.value = `${mode === 'end' ? '终点 B' : '起点 A'} 位于已收录建筑或建筑缓冲区，不能作为候选起降点。`
      return
    }

    const selected = mode === 'start' ? form.start : form.end
    selected.latitude = point.latitude
    selected.longitude = point.longitude
    landingSearchRadiusM.value = inspection.search_radius_m
    if (inspection.state === 'no_road' && roadSource.value === 'local') {
      preview.value = null
      drawCesiumRoute()
      error.value = `${mode === 'end' ? '终点 B' : '起点 A'} 周围 ${inspection.search_radius_m} 米内没有可连接道路。该点仍需现场核验。`
      return
    }

    // A map click changes the route, but should not reset the camera the user
    // is currently inspecting.
    void calculate(false)
    if (mode === 'end' && inspection.state === 'road_nearby') {
      pointHint.value = `终点 B 距已收录道路约 ${inspection.road_distance_m} 米；仅是候选落点，仍需现场核验。`
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '暂时无法检查该点位。'
  } finally {
    checkingPoint.value = false
  }
}

function drawCesiumRoute(flyToRoute = true) {
  const flatView = mapViewMode.value === '2d'
  if (!viewer || !Cesium) return
  selectedViewFrustum = null
  viewer.entities.removeAll()
  viewer.scene.requestRender()
  const start = form.start
  const end = form.end
  const routeAltitude = Math.max(10, Number(form.altitude_m) || 100)
  const routeColor = Cesium.Color.fromCssColorString('#00e5a8')
  const routeShadow = Cesium.Color.fromCssColorString('#062d2d').withAlpha(0.48)
  function addGroundPoint(position: number[], name: string, color: any, pixelSize = 9) {
    const [longitude, latitude] = position
    viewer.entities.add({
      name,
      position: Cesium.Cartesian3.fromDegrees(longitude, latitude, 0),
      point: {
        pixelSize,
        color,
        outlineColor: Cesium.Color.WHITE,
        outlineWidth: 2,
        heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
    })
  }
  function addRange(point: Point, name: string, color: string) {
    viewer.entities.add({
      name,
      position: Cesium.Cartesian3.fromDegrees(point.longitude, point.latitude, 0),
      ellipse: {
        semiMajorAxis: landingSearchRadiusM.value,
        semiMinorAxis: landingSearchRadiusM.value,
        height: 0,
        material: Cesium.Color.fromCssColorString(color).withAlpha(0.08),
        outline: false,
      },
    })
  }
  if (showStartRange.value) addRange(start, '起点 A 接路搜索范围', '#0f6cbd')
  if (showEndRange.value) addRange(end, '终点 B 接路搜索范围', '#5b5fc7')
  viewer.entities.add({
    name: '起点 A',
    position: Cesium.Cartesian3.fromDegrees(start.longitude, start.latitude, 0),
    point: { pixelSize: 15, color: Cesium.Color.fromCssColorString('#0f6cbd'), outlineColor: Cesium.Color.WHITE, outlineWidth: 3 },
    label: { text: 'A  起点', font: '600 14px sans-serif', fillColor: Cesium.Color.fromCssColorString('#242424'), outlineColor: Cesium.Color.WHITE, outlineWidth: 3, style: Cesium.LabelStyle.FILL_AND_OUTLINE, pixelOffset: new Cesium.Cartesian2(0, -27) },
  })
  viewer.entities.add({
    name: '终点 B',
    position: Cesium.Cartesian3.fromDegrees(end.longitude, end.latitude, 0),
    point: { pixelSize: 15, color: Cesium.Color.fromCssColorString('#5b5fc7'), outlineColor: Cesium.Color.WHITE, outlineWidth: 3 },
    label: { text: 'B  终点', font: '600 14px sans-serif', fillColor: Cesium.Color.fromCssColorString('#242424'), outlineColor: Cesium.Color.WHITE, outlineWidth: 3, style: Cesium.LabelStyle.FILL_AND_OUTLINE, pixelOffset: new Cesium.Cartesian2(0, -27) },
  })
  if (!preview.value) return
  const coordinates = preview.value.geometry.coordinates
  const groundPositions = Cesium.Cartesian3.fromDegreesArray(
    coordinates.flatMap(([longitude, latitude]) => [longitude, latitude]),
  )
  const elevatedPositions = Cesium.Cartesian3.fromDegreesArrayHeights(
    coordinates.flatMap(([longitude, latitude], index) => [longitude, latitude, mapViewMode.value === '2d' ? 1 : waypointSettings.value[index]?.altitude_m ?? routeAltitude]),
  )

  // Keep a low-contrast ground trace so the route remains readable against the 2D imagery.
  viewer.entities.add({
    name: '2D 地面参考路线',
    polyline: {
      positions: groundPositions,
      width: 3,
      material: routeShadow,
      clampToGround: true,
    },
  })
  // The route floats at the requested AGL. The glow + core lines create a dimensional neon trace.
  viewer.entities.add({
    name: `3D 航线外光 · ${routeAltitude} m AGL`,
    polyline: {
      positions: elevatedPositions,
      width: 16,
      material: Cesium.PolylineGlowMaterialProperty
        ? new Cesium.PolylineGlowMaterialProperty({ glowPower: 0.22, taperPower: 0.55, color: routeColor.withAlpha(0.72) })
        : routeColor.withAlpha(0.35),
      clampToGround: false,
      depthFailMaterial: routeColor.withAlpha(0.28),
    },
  })
  viewer.entities.add({
    name: `3D 航线主线 · ${routeAltitude} m AGL`,
    polyline: {
      positions: elevatedPositions,
      width: 5,
      material: routeColor,
      clampToGround: false,
      depthFailMaterial: routeColor.withAlpha(0.74),
    },
  })

  // Drop lines connect sampled elevated waypoints back to the flat 2D map.
  const dropStep = Math.max(1, Math.ceil(coordinates.length / 12))
  const waypointIndexes = Array.from(new Set([
    0,
    ...coordinates.map((_, index) => index).filter(index => index % dropStep === 0),
    coordinates.length - 1,
  ]))
  const displayedWaypointIndexes = mapEditMode.value === 'node'
    ? coordinates.map((_, index) => index)
    : waypointIndexes
  displayedWaypointIndexes.forEach((index) => {
    const selected = mapEditMode.value === 'node' && selectedWaypointIndex.value === index
    const [longitude, latitude] = coordinates[index]
    const ground = Cesium.Cartesian3.fromDegrees(longitude, latitude, 0)
    const waypointAltitude = waypointSettings.value[index]?.altitude_m ?? routeAltitude
    const elevated = Cesium.Cartesian3.fromDegrees(longitude, latitude, mapViewMode.value === '2d' ? 1 : waypointAltitude)
    if (!flatView) viewer.entities.add({
      name: `航点 ${index + 1} 垂直投影`,
      polyline: {
        positions: [ground, elevated],
        width: 1.5,
        material: Cesium.Color.fromCssColorString('#e8fff8').withAlpha(0.78),
        clampToGround: false,
        depthFailMaterial: Cesium.Color.fromCssColorString('#e8fff8').withAlpha(0.55),
      },
    })
    // Mirror interior waypoints on the ground so turns remain readable when
    // the elevated route is visually separated from the map surface.
    if (index > 0 && index < coordinates.length - 1) {
      addGroundPoint([longitude, latitude], `地面航点 ${index + 1}`, routeColor.withAlpha(0.88))
    }
    viewer.entities.add({
      name: `航点 ${index + 1}`,
      properties: { waypointIndex: index },
      position: elevated,
      point: { show: flatView || mapEditMode.value !== 'node' || selected, disableDepthTestDistance: Number.POSITIVE_INFINITY, pixelSize: selected ? 28 : index === 0 || index === coordinates.length - 1 ? 13 : 10, color: selected ? Cesium.Color.fromCssColorString('#ffb547') : routeColor, outlineColor: selected ? Cesium.Color.fromCssColorString('#ffd54f') : Cesium.Color.WHITE, outlineWidth: selected ? 4 : 2 },
      label: {
        text: index === 0 ? 'A 3D 起点' : index === coordinates.length - 1 ? 'B 3D 终点' : `W${String(index + 1).padStart(2, '0')}`,
        font: '600 12px sans-serif',
        fillColor: Cesium.Color.WHITE,
        outlineColor: Cesium.Color.fromCssColorString('#12343b'),
        outlineWidth: 3,
        style: Cesium.LabelStyle.FILL_AND_OUTLINE,
        pixelOffset: new Cesium.Cartesian2(0, -19),
        showBackground: true,
        backgroundColor: Cesium.Color.fromCssColorString('#12343b').withAlpha(0.82),
        backgroundPadding: new Cesium.Cartesian2(6, 4),
      },
    })
    if (mapEditMode.value === 'node') {
      const heading = waypointHeading(index)
      viewer.entities.add({
        id: `waypoint-direction-${index}`,
        name: `航点 ${index + 1} 方向箭头`,
        properties: { waypointIndex: index },
        position: elevated,
        billboard: { image: '/svg/jiantou.svg', width: selected ? 30 : 22, height: selected ? 30 : 22, rotation: Cesium.Math.toRadians(heading - 90), verticalOrigin: Cesium.VerticalOrigin.CENTER, horizontalOrigin: Cesium.HorizontalOrigin.CENTER, disableDepthTestDistance: Number.POSITIVE_INFINITY, color: selected ? Cesium.Color.fromCssColorString('#ffb547') : Cesium.Color.WHITE },
      })
      if (selected) {
        const frustum = cameraFrustum(index, routeAltitude)
        selectedViewFrustum = frustum
        const viewColor = Cesium.Color.fromCssColorString('#ffb547').withAlpha(0.82)
        viewer.entities.add({
          id: 'selected-view-direction',
          name: `航点 ${index + 1} 视角方向`,
          polyline: { positions: new Cesium.CallbackProperty(() => selectedViewFrustum ? [selectedViewFrustum.origin, selectedViewFrustum.groundCenter] : [], false), width: 4, material: Cesium.PolylineDashMaterialProperty ? new Cesium.PolylineDashMaterialProperty({ color: viewColor, dashLength: 12 }) : viewColor, clampToGround: false },
        })
        if (!flatView) frustum.faces.forEach((face, faceIndex) => viewer.entities.add({
          id: `selected-view-face-${faceIndex}`,
          name: `航点 ${index + 1} 视锥体侧面 ${faceIndex + 1}`,
          polygon: { hierarchy: new Cesium.CallbackProperty(() => new Cesium.PolygonHierarchy(selectedViewFrustum?.faces[faceIndex] ?? []), false), perPositionHeight: true, arcType: Cesium.ArcType.NONE, material: Cesium.Color.fromCssColorString('#72e6c2').withAlpha(0.22), outline: true, outlineColor: viewColor.withAlpha(0.62), outlineWidth: 1 },
        }))
        viewer.entities.add({ id: 'selected-view-rays', name: `航点 ${index + 1} 视场四角连线`, polyline: { positions: new Cesium.CallbackProperty(() => selectedViewFrustum?.ground.flatMap((corner) => [selectedViewFrustum!.origin, corner]) ?? [], false), width: 2, material: viewColor, clampToGround: false } })
        viewer.entities.add({ id: 'selected-view-border', name: `航点 ${index + 1} 地面视场边框`, polyline: { positions: new Cesium.CallbackProperty(() => selectedViewFrustum ? [...selectedViewFrustum.ground, selectedViewFrustum.ground[0]] : [], false), width: 2, material: Cesium.Color.fromCssColorString('#36dfba').withAlpha(0.78), clampToGround: false } })
        viewer.entities.add({
          name: `航点 ${index + 1} 拖动镜头`,
          properties: { cameraIndex: index },
          position: frustum.origin,
          point: { pixelSize: 13, color: Cesium.Color.fromCssColorString('#ffb547'), outlineColor: Cesium.Color.WHITE, outlineWidth: 2, disableDepthTestDistance: Number.POSITIVE_INFINITY },
          label: { text: '镜头', font: '600 11px sans-serif', fillColor: Cesium.Color.WHITE, outlineColor: Cesium.Color.fromCssColorString('#12343b'), outlineWidth: 3, style: Cesium.LabelStyle.FILL_AND_OUTLINE, pixelOffset: new Cesium.Cartesian2(0, -18) },
        })
      }
    }
  })

  if (routeAssessment.value && weatherCoordinates.value) {
    const colors: Record<string, string> = { no_trigger: '#0f6cbd', attention: '#b66a00', elevated: '#b42318', incomplete: '#6e7781' }
    for (const segment of routeAssessment.value.weather_risk.segments) {
      const segmentColor = Cesium.Color.fromCssColorString(colors[segment.level] || '#6e7781')
      viewer.entities.add({ name: `气象初筛第 ${segment.index + 1} 段 · ${segment.level}（非街道级预报）`,
        polyline: { positions: Cesium.Cartesian3.fromDegreesArrayHeights([...segment.start, routeAltitude, ...segment.end, routeAltitude]), width: 5,
          material: segmentColor, clampToGround: false,
          depthFailMaterial: segmentColor.withAlpha(0.6) } })
      if (mapEditMode.value !== 'node') {
        addGroundPoint(segment.start, `气象初筛地面点 ${segment.index + 1} 起`, segmentColor.withAlpha(0.9), 8)
        addGroundPoint(segment.end, `气象初筛地面点 ${segment.index + 1} 止`, segmentColor.withAlpha(0.9), 8)
      }
    }
  }
  const centerLongitude = (start.longitude + end.longitude) / 2
  const centerLatitude = (start.latitude + end.latitude) / 2
  const cameraHeight = Math.max(760, preview.value.straight_distance_km * 3200, routeAltitude * 5.5)
  if (flyToRoute) {
    viewer.camera.flyTo({
      destination: Cesium.Cartesian3.fromDegrees(centerLongitude, centerLatitude, cameraHeight),
      orientation: { heading: 0, pitch: Cesium.Math.toRadians(mapViewMode.value === '2d' ? -90 : -34), roll: 0 },
      duration: 0.8,
    })
  }
}

function initMap() {
  Cesium = (window as any).Cesium
  if (!Cesium || !mapElement.value) return
  try {
    viewer = new Cesium.Viewer(mapElement.value, {
      baseLayer: false,
      terrainProvider: new Cesium.EllipsoidTerrainProvider(),
      requestRenderMode: true,
      maximumRenderTimeChange: Number.POSITIVE_INFINITY,
      animation: false,
      timeline: false,
      baseLayerPicker: false,
      geocoder: false,
      homeButton: false,
      navigationHelpButton: false,
      sceneModePicker: false,
      fullscreenButton: false,
      infoBox: false,
      selectionIndicator: false,
    })
    viewer.resolutionScale = 1
    viewer.imageryLayers.addImageryProvider(new Cesium.OpenStreetMapImageryProvider({
      url: 'https://tile.openstreetmap.org/',
      credit: new Cesium.Credit('<a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">© OpenStreetMap contributors</a>', true),
    }))
    viewer.scene.globe.depthTestAgainstTerrain = false
    viewer.camera.setView({
      destination: Cesium.Cartesian3.fromDegrees(119.95050, 31.67980, 1100),
      orientation: { heading: 0, pitch: Cesium.Math.toRadians(mapViewMode.value === '2d' ? -90 : -34), roll: 0 },
    })
    const clickHandler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas)
    const pickEntity = (position: any) => {
      const picked = viewer.scene.pick(position)
      return picked?.id ?? null
    }
    const propertyValue = (property: any) => property?.getValue ? property.getValue(Cesium.JulianDate.now()) : property
    const pickWaypointEntity = (position: any) => {
      const picks = viewer.scene.drillPick ? viewer.scene.drillPick(position, 12) : []
      for (const pick of picks) {
        const entity = pick?.id
        if (entity?.properties?.waypointIndex || entity?.properties?.cameraIndex) return entity
      }
      return pickEntity(position)
    }
    clickHandler.setInputAction((event: any) => {
      const entity = pickWaypointEntity(event.position)
      const cameraProperty = entity?.properties?.cameraIndex
      if (mapEditMode.value === 'node' && cameraProperty) {
        draggingCameraIndex.value = Number(propertyValue(cameraProperty))
        viewer.camera.cancelFlight()
        mapCameraInputsBeforeDrag = viewer.scene.screenSpaceCameraController.enableInputs
        viewer.scene.screenSpaceCameraController.enableInputs = false
      }
    }, Cesium.ScreenSpaceEventType.LEFT_DOWN)
    clickHandler.setInputAction((event: any) => {
      if (draggingCameraIndex.value === null) return
      const cartesian = viewer.camera.pickEllipsoid(event.endPosition, viewer.scene.globe.ellipsoid)
      if (!cartesian) return
      const point = Cesium.Cartographic.fromCartesian(cartesian)
      const index = draggingCameraIndex.value
      const coordinates = preview.value?.geometry.coordinates ?? []
      const origin = coordinates[index]
      if (!origin) return
      const latitude = Number(Cesium.Math.toDegrees(point.latitude))
      const longitude = Number(Cesium.Math.toDegrees(point.longitude))
      const heading = Math.atan2(Math.sin((longitude - origin[0]) * Math.PI / 180) * Math.cos(latitude * Math.PI / 180), Math.cos(origin[1] * Math.PI / 180) * Math.sin(latitude * Math.PI / 180) - Math.sin(origin[1] * Math.PI / 180) * Math.cos(latitude * Math.PI / 180) * Math.cos((longitude - origin[0]) * Math.PI / 180)) * 180 / Math.PI
      setSelectedWaypointOrientation(heading, undefined, true)
    }, Cesium.ScreenSpaceEventType.MOUSE_MOVE)
    clickHandler.setInputAction(finishMapCameraDrag, Cesium.ScreenSpaceEventType.LEFT_UP)
    clickHandler.setInputAction((event: any) => {
      if (suppressNextMapClick.value) {
        suppressNextMapClick.value = false
        return
      }
      const entity = pickWaypointEntity(event.position)
      const waypointProperty = entity?.properties?.waypointIndex
      if (waypointProperty) {
        selectWaypoint(Number(propertyValue(waypointProperty)))
        return
      }
      if (mapEditMode.value === 'node') {
        return
      }
      const cartesian = viewer.camera.pickEllipsoid(event.position, viewer.scene.globe.ellipsoid)
      if (!cartesian) return
      const point = Cesium.Cartographic.fromCartesian(cartesian)
      void chooseMapPoint({
        latitude: Number(Cesium.Math.toDegrees(point.latitude).toFixed(5)),
        longitude: Number(Cesium.Math.toDegrees(point.longitude).toFixed(5)),
      })
    }, Cesium.ScreenSpaceEventType.LEFT_CLICK)
    mapReady.value = true
    drawCesiumRoute()
  } catch {
    mapReady.value = false
    viewer?.destroy()
    viewer = null
  }
}

function initCameraPreview() {
  if (!Cesium || !cameraPreviewElement.value || cameraPreviewViewer) return
  try {
    cameraPreviewViewer = new Cesium.Viewer(cesiumCameraElement.value ?? cameraPreviewElement.value, {
      baseLayer: false,
      terrainProvider: new Cesium.EllipsoidTerrainProvider(),
      requestRenderMode: true,
      maximumRenderTimeChange: Number.POSITIVE_INFINITY,
      animation: false,
      timeline: false,
      baseLayerPicker: false,
      geocoder: false,
      homeButton: false,
      navigationHelpButton: false,
      sceneModePicker: false,
      fullscreenButton: false,
      infoBox: false,
      selectionIndicator: false,
    })
    cameraPreviewViewer.imageryLayers.addImageryProvider(new Cesium.OpenStreetMapImageryProvider({ url: 'https://tile.openstreetmap.org/' }))
    cameraPreviewViewer.scene.globe.depthTestAgainstTerrain = false
    const controller = cameraPreviewViewer.scene.screenSpaceCameraController
    controller.enableRotate = false
    controller.enableTranslate = false
    controller.enableZoom = false
    controller.enableTilt = false
    controller.enableLook = false
    controller.inertiaSpin = 0
    controller.inertiaTranslate = 0
    controller.inertiaZoom = 0
    cameraPreviewViewer.cesiumWidget.screenSpaceEventHandler.removeInputAction(Cesium.ScreenSpaceEventType.LEFT_DOUBLE_CLICK)
    cameraPreviewViewer.resolutionScale = 1
    cameraPreviewReady.value = true
    void nextTick(() => {
      cameraPreviewViewer?.resize()
      cameraPreviewViewer?.scene.requestRender()
    })
  } catch {
    cameraPreviewReady.value = false
    cameraPreviewViewer?.destroy()
    cameraPreviewViewer = null
  }
}

onMounted(() => {
  window.addEventListener('keydown', handleWaypointKeydown)
  window.addEventListener('blur', cancelCameraPreviewDrag)
  window.addEventListener('blur', finishMapCameraDrag)
  window.addEventListener('pointerup', finishMapCameraDrag)
  scheduleDepartureClock()
  void calculate()
  if ((window as any).Cesium) {
    initMap()
  }
  else window.addEventListener('load', initMap, { once: true })
})
onBeforeUnmount(() => {
  window.removeEventListener('keydown', handleWaypointKeydown)
  window.removeEventListener('blur', cancelCameraPreviewDrag)
  window.removeEventListener('blur', finishMapCameraDrag)
  window.removeEventListener('pointerup', finishMapCameraDrag)
  cancelCameraPreviewDrag()
  finishMapCameraDrag()
  if (departureClockTimer) clearTimeout(departureClockTimer)
  stopColumnResize()
  cancelCalculation()
  if (waypointRenderFrame !== null) cancelAnimationFrame(waypointRenderFrame)
  window.removeEventListener('load', initMap)
  if (viewer && !viewer.isDestroyed()) viewer.destroy()
  if (cameraPreviewViewer && !cameraPreviewViewer.isDestroyed()) cameraPreviewViewer.destroy()
})
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <div class="brand"><span class="brand-mark" aria-hidden="true"><span class="svg-icon icon-route"></span></span><div class="brand-copy"><strong>低空飞行决策工作台</strong><small>航线规划与天气评估</small></div></div>
      <div class="top-actions"><span class="prototype-tag"><span class="svg-icon icon-route" aria-hidden="true"></span>道路参考路线</span><span class="connection">原型阶段</span></div>
    </header>

    <nav class="workspace-tabs" aria-label="工作区面板">
      <button type="button" :aria-pressed="workspacePanel === 'settings'" aria-controls="mission-sidebar" @click="workspacePanel = 'settings'">航线设置</button>
      <button type="button" :aria-pressed="workspacePanel === 'map'" aria-controls="route-map" @click="workspacePanel = 'map'">航线地图</button>
      <button type="button" :aria-pressed="workspacePanel === 'overview'" aria-controls="mission-insights" @click="workspacePanel = 'overview'">任务概览</button>
    </nav>

    <main class="layout" :style="layoutStyle">
      <aside id="mission-sidebar" class="sidebar" :class="{ 'panel-active': workspacePanel === 'settings' }" aria-label="航线设置">
        <div class="section-heading"><span class="eyebrow"><span class="svg-icon icon-route" aria-hidden="true"></span>航线设置</span><h1>规划一次飞行</h1><PlaceSearch :disabled="loading || checkingPoint" /></div>

        <form class="route-form" @submit.prevent="calculate" @invalid.capture="revealInvalidSetting">
          <nav class="settings-pages" aria-label="航线设置分组"><button type="button" :aria-pressed="settingsPage === 'points'" @click="settingsPage = 'points'">起点与终点</button><button type="button" :aria-pressed="settingsPage === 'mission'" @click="settingsPage = 'mission'">飞行参数</button></nav>
          <div class="settings-fields">
          <div class="settings-group" data-settings-page="points" :class="{ 'settings-active': settingsPage === 'points' }">
           <div class="point-heading"><span class="point-mark point-a">A</span><div><strong>起点</strong><small>{{ formatted(form.start) }}</small></div><button type="button" class="range-toggle" :class="{ active: showStartRange }" :aria-pressed="showStartRange" :disabled="loading || checkingPoint" title="显示或隐藏 35 米接路搜索范围" @click="toggleRange('start')"><span class="svg-icon icon-eye" aria-hidden="true"></span><span>范围：{{ showStartRange ? '开' : '关' }}</span></button></div>
          <div class="coordinate-row"><label>纬度<input v-model.number="form.start.latitude" type="number" step="0.00001" min="-90" max="90" required :disabled="loading" /></label><label>经度<input v-model.number="form.start.longitude" type="number" step="0.00001" min="-180" max="180" required :disabled="loading" /></label></div>

          <div class="route-divider" aria-hidden="true"></div>

           <div class="point-heading"><span class="point-mark point-b">B</span><div><strong>终点</strong><small>{{ formatted(form.end) }}</small></div><button type="button" class="range-toggle" :class="{ active: showEndRange }" :aria-pressed="showEndRange" :disabled="loading || checkingPoint" title="显示或隐藏 35 米接路搜索范围" @click="toggleRange('end')"><span class="svg-icon icon-eye" aria-hidden="true"></span><span>范围：{{ showEndRange ? '开' : '关' }}</span></button></div>
          <div class="coordinate-row"><label>纬度<input v-model.number="form.end.latitude" type="number" step="0.00001" min="-90" max="90" required :disabled="loading" /></label><label>经度<input v-model.number="form.end.longitude" type="number" step="0.00001" min="-180" max="180" required :disabled="loading" /></label></div>
          </div>

          <div class="settings-group" data-settings-page="mission" :class="{ 'settings-active': settingsPage === 'mission' }">
          <div class="form-line"></div>
          <div class="coordinate-row"><label>离地高度 AGL · 米<input v-model.number="form.altitude_m" type="number" min="0" max="1000" required :disabled="loading" /></label><label>巡航速度 · km/h<input v-model.number="form.speed_kmh" type="number" min="1" max="200" step="0.5" required :disabled="loading" /></label></div>
          <small class="form-scope-note">目前高度风覆盖 10–100 米；范围外会标记缺测。</small>
          <label class="departure-field">出发时刻 · 北京时间<input v-model="departureBeijing" type="datetime-local" :min="latestDepartureBeijing" required @input="markDepartureManual" /><small>{{ departureAutoManaged ? '默认跟随当前北京时间，每分钟更新一次。' : '已使用手动选择的出发时间。' }}沿线 ETA 按填写速度估算，不含风对地速的影响。</small></label>
          <div class="clearance-row"><label>建筑缓冲 · 米（演示）<input v-model.number="form.clearance_m" type="number" min="0" max="20" step="0.5" required :disabled="loading || roadSource === 'online'" /></label><span>{{ roadSource === 'local' ? '用于排除靠近建筑的道路段' : '在线机动车路线不使用此参数' }}</span></div>
          </div>
          </div>
           <fluent-button class="primary-button" appearance="primary" type="submit" :disabled="loading"><span class="svg-icon" :class="loading ? 'icon-refresh is-spinning' : 'icon-route'" aria-hidden="true"></span><span>{{ loading ? '计算中…' : '计算道路路线' }}</span></fluent-button>
          <p v-if="error" class="form-error" role="alert">{{ error }}</p>
        </form>
      </aside>

      <section id="route-map" class="map-panel" :class="{ 'panel-active': workspacePanel === 'map' }" aria-label="航线地图">
        <div ref="mapElement" class="cesium-map" :class="{ visible: mapReady && baseMap === 'current' }"></div>
        <EarthMap v-if="earthMapMounted" ref="earthMapRef" :active="baseMap === 'earth'" :view-mode="mapViewMode" :start="form.start" :end="form.end" :route="earthRoute" :selected-index="selectedWaypointIndex" :edit-mode="mapEditMode" :footprint="earthFootprint" :weather-segments="routeAssessment?.weather_risk.segments ?? []" :show-start-range="showStartRange" :show-end-range="showEndRange" :range-meters="landingSearchRadiusM" @select-waypoint="selectWaypoint" @choose-point="chooseMapPoint" @rotate-waypoint="setSelectedWaypointOrientation($event, undefined, true)" />
        <div v-if="!mapReady && baseMap === 'current'" class="map-fallback">
          <div class="grid-lines"></div>
          <p>地图底图加载中</p><small>道路路线按所选数据源计算</small>
        </div>
         <div class="map-top"><div class="map-title-group"><span class="map-badge"><span class="svg-icon icon-layers" aria-hidden="true"></span>{{ (baseMap === 'earth' ? '地球模式' : '当前底图') + (mapViewMode === '3d' ? ' · 3D 航线' : ' · 2D 平面') }}</span><span class="map-mode-chip">{{ form.altitude_m }} m AGL</span><div class="map-source-switch base-map-switch" aria-label="底图"><button type="button" :class="{ active: baseMap === 'current' }" :aria-pressed="baseMap === 'current'" @click="chooseBaseMap('current')">当前底图</button><button type="button" :class="{ active: baseMap === 'earth' }" :aria-pressed="baseMap === 'earth'" @click="chooseBaseMap('earth')">地球模式</button></div><div class="map-source-switch" aria-label="路线数据源"><button type="button" :class="{ active: roadSource === 'local' }" :aria-pressed="roadSource === 'local'" :disabled="loading || checkingPoint" @click="chooseRoadSource('local')">本地</button><button type="button" :class="{ active: roadSource === 'online' }" :aria-pressed="roadSource === 'online'" :disabled="loading || checkingPoint" @click="chooseRoadSource('online')">在线</button></div><div class="map-edit-switch" aria-label="编辑对象"><button type="button" :class="{ active: mapEditMode === 'route' }" :aria-pressed="mapEditMode === 'route'" @click="mapEditMode = 'route'">路径 / A-B</button><button type="button" :class="{ active: mapEditMode === 'node' }" :aria-pressed="mapEditMode === 'node'" @click="mapEditMode = 'node'">节点 / 视角</button></div></div><span class="map-caption">{{ checkingPoint ? '正在检查点位…' : mapEditMode === 'node' ? '点击节点选中，拖动橙色镜头调整朝向' : `地图点击可设置 ${mapMode === 'start' ? '起点 A' : '终点 B'}` }}</span></div>
        <div class="map-view-switch map-source-switch view-mode-switch" aria-label="地图视图"><button type="button" :class="{ active: mapViewMode === '2d' }" :aria-pressed="mapViewMode === '2d'" @click="chooseMapViewMode('2d')">2D 平面</button><button type="button" :class="{ active: mapViewMode === '3d' }" :aria-pressed="mapViewMode === '3d'" @click="chooseMapViewMode('3d')">3D 视角</button></div>
        <div v-if="error" class="map-alert" role="alert">{{ error }}</div>
        <div v-else-if="pointHint" class="map-point-hint" role="status">{{ pointHint }}</div>
         <div class="map-bottom"><div class="map-switch"><button :class="{ active: mapMode === 'start' }" :disabled="checkingPoint || mapEditMode !== 'route'" @click="mapMode = 'start'"><span class="svg-icon icon-map-pin" aria-hidden="true"></span>设置起点 A</button><button :class="{ active: mapMode === 'end' }" :disabled="checkingPoint || mapEditMode !== 'route'" @click="mapMode = 'end'"><span class="svg-icon icon-map-pin" aria-hidden="true"></span>设置终点 B</button></div><span class="map-radius"><span class="svg-icon icon-map-pin" aria-hidden="true"></span>接路判定 {{ landingSearchRadiusM }} m</span><span class="map-scale"><span class="svg-icon icon-wind" aria-hidden="true"></span>任务高度 {{ form.altitude_m }} m</span></div>
        <div v-if="mapEditMode === 'node' && selectedWaypointIndex !== null && selectedWaypointSetting()" class="node-controller" aria-label="航点控制器"><div class="node-controller-head"><strong>节点 {{ selectedWaypointIndex + 1 }} · 视角控制</strong><span>{{ Math.round(waypointHeading(selectedWaypointIndex)) }}° · {{ selectedWaypointSetting()?.altitude_m ?? form.altitude_m }}m</span></div><div class="node-controller-body"><div class="wasd-pad" aria-label="WASD 平面移动控制"><button type="button" @click="moveSelectedWaypoint(0, 1)">W</button><button type="button" @click="moveSelectedWaypoint(-1, 0)">A</button><button type="button" @click="moveSelectedWaypoint(0, -1)">S</button><button type="button" @click="moveSelectedWaypoint(1, 0)">D</button></div><div class="node-dial"><button type="button" aria-label="逆时针旋转" @click="rotateSelectedWaypoint(-15)">↺</button><strong>{{ Math.round(waypointHeading(selectedWaypointIndex)) }}°</strong><button type="button" aria-label="顺时针旋转" @click="rotateSelectedWaypoint(15)">↻</button></div><div class="node-axis"><button type="button" @click="rotateSelectedWaypoint(0, 5)">镜头上</button><button type="button" @click="rotateSelectedWaypoint(0, -5)">镜头下</button><button type="button" @click="adjustSelectedWaypoint({ altitude_m: Math.min(1500, (selectedWaypointSetting()?.altitude_m ?? form.altitude_m) + 5) })">高度 +</button><button type="button" @click="adjustSelectedWaypoint({ altitude_m: Math.max(1, (selectedWaypointSetting()?.altitude_m ?? form.altitude_m) - 5) })">高度 −</button></div></div><small>W/S 平面前后 ±1m，A/D 平面左右 ±1m，Q/E 航向 ±5°，Z/C 俯仰 ±5°可直接调整水平朝向。</small></div>
        <div v-if="routeAssessment" class="weather-map-legend">沿线初筛：蓝 · 未触发，橙 · 关注，红 · 较强，灰 · 缺测<br />基于 0.25° 场，不是街道级风险，也不代表飞行许可。</div>
        <div class="camera-preview" :class="{ visible: mapEditMode === 'node' && selectedWaypointIndex !== null && (baseMap === 'earth' || cameraPreviewReady) }" aria-label="当前节点相机视角预览"><div ref="cameraPreviewElement" class="camera-preview-map" :data-base-map="baseMap" @pointerdown.capture="handleCameraPreviewPointerDown" @pointermove="handleCameraPreviewPointerMove" @pointerup="handleCameraPreviewPointerEnd" @pointercancel="handleCameraPreviewPointerEnd" @lostpointercapture="handleCameraPreviewPointerEnd"><div ref="cesiumCameraElement" :style="{ position: 'absolute', inset: '0', visibility: baseMap === 'current' ? 'visible' : 'hidden' }"></div><BaiduCameraPreview v-if="baseMap === 'earth' && mapEditMode === 'node' && selectedWaypointIndex !== null" :active="baseMap === 'earth' && mapEditMode === 'node' && selectedWaypointIndex !== null" :pose="selectedCameraPose" /></div><div class="camera-preview-head"><strong>当前视角</strong><span v-if="selectedWaypointIndex !== null">节点 {{ selectedWaypointIndex + 1 }} · {{ Math.round(selectedWaypointSetting()?.gimbal_pitch_angle ?? 0) }}°</span></div><small>地图模拟视角 · 不是实时相机视频</small></div>
        <div v-if="loading" class="map-loading-overlay">
          <div class="map-loading-card">
            <button type="button" class="map-loading-cancel" aria-label="取消路线计算" title="取消" @click="cancelCalculation">×</button>
            <span class="map-loading-spinner" aria-hidden="true"></span>
            <strong role="status" aria-live="polite">加载中…</strong>
            <span>{{ roadSource === 'online' ? '正在获取在线道路路线' : '正在计算本地道路路线' }}</span>
          </div>
        </div>
      </section>

      <div class="column-resizer column-resizer-left" role="separator" aria-orientation="vertical" aria-label="调整航线设置栏宽度" aria-controls="mission-sidebar route-map" :aria-valuenow="Math.round(sidebarWidth)" aria-valuemin="260" :aria-valuemax="Math.round(widthLimit('left'))" tabindex="0" title="拖动调整航线设置栏宽度，双击恢复默认" :style="{ left: `${sidebarWidth}px` }" @pointerdown="startColumnResize('left', $event)" @dblclick="resetColumnWidths" @keydown="resizeColumnByKeyboard('left', $event)"></div>
      <div class="column-resizer column-resizer-right" role="separator" aria-orientation="vertical" aria-label="调整任务概览栏宽度" aria-controls="route-map mission-insights" :aria-valuenow="Math.round(insightsWidth)" :aria-valuemin="minimumWidth('right')" :aria-valuemax="Math.round(widthLimit('right'))" tabindex="0" title="拖动调整任务概览栏宽度，双击恢复默认" :style="{ right: `${insightsWidth}px` }" @pointerdown="startColumnResize('right', $event)" @dblclick="resetColumnWidths" @keydown="resizeColumnByKeyboard('right', $event)"></div>

      <aside id="mission-insights" class="insights" :class="{ 'panel-active': workspacePanel === 'overview' }" aria-label="任务概览" tabindex="0" @change="markWaypointEdited">
        <div class="insights-header"><span class="eyebrow"><span class="svg-icon icon-square-check" aria-hidden="true"></span>任务状态</span><h2>先看天气结论</h2><p>先看结论，需要细节时再展开。</p></div>
        <section class="overview-hero" aria-label="天气结论概览">
        <div class="mission-facts"><div><span class="fact-label"><span class="svg-icon icon-route" aria-hidden="true"></span>路线长度</span><strong>{{ distance }}</strong></div><div><span class="fact-label"><span class="svg-icon icon-refresh" aria-hidden="true"></span>预计用时</span><strong>{{ elapsed }}</strong></div><div><span class="fact-label"><span class="svg-icon icon-map-pin" aria-hidden="true"></span>计划出发</span><strong>{{ departureBeijing.replace('T', ' ') }}</strong><small>北京时间</small></div></div>
         <div class="decision" :class="decisionTone" aria-live="polite"><span class="decision-kicker"><span class="svg-icon icon-alert" aria-hidden="true"></span>气象初筛</span><strong>{{ decisionTitle }}</strong><p>{{ decisionDescription }}</p><small>这里只反映已覆盖的天气指标，不等于放飞许可。</small><details v-if="routeAssessment"><summary><span class="svg-icon icon-eye" aria-hidden="true"></span>查看判断依据</summary><div v-for="check in weatherChecks" :key="check.key" class="decision-check"><b>{{ check.label }}</b><span :class="check.state === 'ready' ? 'check-ready' : 'check-pending'">{{ check.state === 'ready' ? '资料已就绪' : check.state === 'no_trigger' ? '未触发当前阈值' : check.state === 'elevated' ? '较强提示' : check.state === 'attention' ? '需要留意' : '资料待补齐' }}</span><small>{{ check.detail }}</small></div></details></div>
        </section>
        <details class="overview-group" open>
           <summary><span><span class="svg-icon icon-cloud-rain" aria-hidden="true"></span>沿线天气检查</span><small>风、降雨、阵风和能见度</small></summary>
          <div class="overview-group-body overview-weather">
            <WeatherPanels :altitude-m="form.altitude_m" :start="form.start" :end="form.end" @job-ready="weatherJobId = $event" @state-changed="weatherState = $event" />
            <RouteWeatherPanel :job-id="weatherJobId" :coordinates="weatherCoordinates" :departure-beijing="departureBeijing" :speed-kmh="form.speed_kmh" :altitude-m="form.altitude_m" :limits="activeLimits" @assessed="routeAssessment = $event" @phase="routePhase = $event" />
          </div>
        </details>
         <details class="overview-group"><summary><span><span class="svg-icon icon-route" aria-hidden="true"></span>调整出发时间或路线</span><small>可选参考，不会自动替换当前设置</small></summary><div class="overview-group-body overview-planning"><WeatherWindowsPanel :job-id="weatherJobId" :coordinates="weatherCoordinates" :departure-beijing="departureBeijing" :speed-kmh="form.speed_kmh" :altitude-m="form.altitude_m" :limits="activeLimits" @choose="chooseDeparture" @state="windowPhase = $event" /><RoadWeatherComparison :job-id="weatherJobId" :can-compare="!!weatherCoordinates" :current-coordinates="weatherCoordinates" :start="form.start" :end="form.end" :road-source="roadSource" :clearance-m="form.clearance_m" :departure-beijing="departureBeijing" :speed-kmh="form.speed_kmh" :altitude-m="form.altitude_m" :limits="activeLimits" @choose="chooseRoad" @state="comparisonPhase = $event" /></div></details>
         <details class="overview-group"><summary><span><span class="svg-icon icon-layers" aria-hidden="true"></span>天气规则和技术状态</span><small>仅在需要核对时查看</small></summary><div class="overview-group-body"><details class="limits-settings"><summary>天气初筛阈值</summary><label class="limits-toggle"><input v-model="customLimitsEnabled" type="checkbox" /> 填写自定义气象阈值</label><p>关闭时使用演示阈值。阈值用于天气提示与比较，参考分不是事故概率。</p><template v-if="customLimitsEnabled"><label>阈值名称<input v-model="customLimits.name" maxlength="80" /></label><div class="coordinate-row"><label>最大高度风 · m/s<input v-model.number="customLimits.max_wind_m_s" type="number" min="0.1" max="200" step="0.1" required /></label><label>最大侧风 · m/s<input v-model.number="customLimits.max_crosswind_m_s" type="number" min="0.1" max="200" step="0.1" required /></label></div><div class="coordinate-row"><label>最大逆风 · m/s<input v-model.number="customLimits.max_headwind_m_s" type="number" min="0.1" max="200" step="0.1" required /></label><label>最大地面阵风 · m/s<input v-model.number="customLimits.max_gust_m_s" type="number" min="0.1" max="200" step="0.1" required /></label></div><div class="coordinate-row"><label>最大雨量 · mm/6h<input v-model.number="customLimits.max_rain_6h_mm" type="number" min="0.1" max="500" step="0.1" required /></label><label>最低地面能见度 · m<input v-model.number="customLimits.min_visibility_m" type="number" min="1" max="100000" step="1" required /></label></div></template></details><div class="pipeline"><h3>系统状态</h3><div><span>01</span><p>{{ alignmentProgress }}</p></div><div><span>02</span><p>{{ routeAssessment ? decisionTitle : '沿线初筛等待资料' }}</p></div><div><span>03</span><p>{{ planningProgress }}</p></div></div></div></details>
         <div v-if="preview" class="road-summary"><strong><span class="svg-icon icon-route" aria-hidden="true"></span>当前路线：{{ preview.road_names.join(' → ') || '未命名道路' }}</strong><span>{{ distance }} · 预计用时 {{ elapsed }}</span><details><summary><span class="svg-icon icon-eye" aria-hidden="true"></span>查看路线数据</summary><small>直线 {{ preview.straight_distance_km.toFixed(2) }} km · {{ preview.building_clearance_m === null ? '在线模式未检查建筑缓冲' : `建筑缓冲 ${preview.building_clearance_m} m` }}</small><small>A/B 到道路分别约 {{ preview.start_snap_m }} m / {{ preview.end_snap_m }} m</small><small>{{ preview.data_source }}</small><small>路线数据 © <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap contributors</a></small></details><div class="route-export"><label>DJI 机型模板<select v-model="djiModel"><option value="fly">DJI Fly · Air 3 / Air 3S / Mini 4 Pro</option><option value="m30">DJI Pilot 2 · M30 / M30T</option><option value="m3e">DJI Pilot 2 · Mavic 3E / 3T / 3M</option></select></label><details class="waypoint-editor"><summary>逐点设置高度和面向（{{ waypointSettings.length }} 个点）</summary><p class="waypoint-editor-note">高度为相对起飞点高度；机头/镜头水平朝向以真北为 0°，摄像头俯仰角 0° 为水平，负数向下。Air 3/Air 3S 的镜头水平偏航是否独立于机头由 DJI Fly 和机型能力决定。</p><div v-for="(setting, index) in waypointSettings" :key="index" class="waypoint-row"><strong>点 {{ index + 1 }}</strong><label>高度（m）<input v-model.number="setting.altitude_m" type="number" min="1" max="1500" step="1" /></label><label>机头方向<select v-model="setting.heading_mode"><option value="followWayline">沿航线</option><option value="fixed">固定角度</option><option value="smoothTransition">平滑转向</option><option value="manually">手动控制</option></select></label><label v-if="setting.heading_mode === 'fixed' || setting.heading_mode === 'smoothTransition'">角度（°）<input v-model.number="setting.heading_angle" type="number" step="1" /></label><label>摄像头俯仰（°）<input v-model.number="setting.gimbal_pitch_angle" type="number" min="-90" max="45" step="1" /></label></div></details><button type="button" class="weather-refresh" :disabled="kmzLoading || loading" @click="exportDjiKmz"><span class="svg-icon icon-download" aria-hidden="true"></span>{{ kmzLoading ? '正在生成 KMZ…' : '导出 DJI KMZ 草稿' }}</button><small>按相对起飞点高度生成。路线折点会先抽稀，减少 DJI 中高度标签和航点标记重叠。{{ roadSource === 'online' ? '当前是在线机动车道路参考，不应直接作为飞行线。' : '当前是道路走廊参考，不代表已核实飞行航线。' }}{{ djiModel === 'fly' ? 'DJI Fly 需先创建一条占位航线，再用相同 UUID 文件名替换 waypoint 目录中的 KMZ。' : '导入 DJI Pilot 2 后仍需核对机型、动作、返航、空域和现场条件。' }}文件不是放飞许可。</small><small v-if="djiModel === 'fly'">Air 3/Air 3S 使用 DJI Fly 消费级 WPML；当前通用机型枚举按 RC 2 航线格式生成，若设备拒绝文件需要用设备原生占位 KMZ 校验。</small><small v-if="kmzError" class="form-error" role="alert">{{ kmzError }}</small></div></div>
      </aside>
    </main>
  </div>
</template>

<style scoped>
.cesium-map { visibility: hidden; pointer-events: none; }
.cesium-map.visible { visibility: visible; pointer-events: auto; }
.map-top { flex-wrap: wrap; z-index: 3; }
.map-bottom { z-index: 3; }
.map-title-group { min-width: 0; }
.base-map-switch button { white-space: nowrap; }
.view-mode-switch button { white-space: nowrap; }
.map-view-switch { position: absolute; top: 72px; left: 20px; z-index: 4; pointer-events: auto; }
@media (max-width: 720px) {
  .map-view-switch { top: 112px; left: 14px; }
}
@media (max-width: 720px) {
  .map-alert, .map-point-hint { top: 150px; }
}
</style>

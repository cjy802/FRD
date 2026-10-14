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
import { createFlightTimeline, sampleFlight, type FlightSample } from './flightPlayback'

type Point = { latitude: number; longitude: number }
type MajorPoint = { id: string; latitude: number | null; longitude: number | null }
type LocationFeedback = { target: string; message: string; error?: boolean }
const workspacePanel = ref<'settings' | 'map' | 'overview'>('overview')
const settingsPage = ref<'points' | 'mission'>('points')
type LandingCheck = {
  state: 'mapped_building' | 'road_nearby' | 'no_road'
  search_radius_m: number
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
const anchors = ref<MajorPoint[]>([
  { id: 'anchor-1', latitude: form.start.latitude, longitude: form.start.longitude },
  { id: 'anchor-2', latitude: form.end.latitude, longitude: form.end.longitude },
])
let nextAnchorNumber = 3
const selectedAnchorId = ref('anchor-1')
function majorPointLabel(index: number) {
  let value = index + 1
  let label = ''
  while (value > 0) {
    value -= 1
    label = String.fromCharCode(65 + value % 26) + label
    value = Math.floor(value / 26)
  }
  return label
}
function majorPointTitle(index: number) {
  return index === 0 ? '起点' : index === anchors.value.length - 1 ? '终点' : '途经点'
}
function validMajorPoint(anchor: MajorPoint | undefined): anchor is MajorPoint & Point {
  return !!anchor && typeof anchor.latitude === 'number' && Number.isFinite(anchor.latitude)
    && Math.abs(anchor.latitude) <= 90 && typeof anchor.longitude === 'number'
    && Number.isFinite(anchor.longitude) && Math.abs(anchor.longitude) <= 180
}
const allAnchorsReady = computed(() => anchors.value.length >= 2 && anchors.value.every(validMajorPoint))
const anchorChoices = computed(() => anchors.value.map((anchor, index) => ({ id: anchor.id, label: majorPointLabel(index), title: majorPointTitle(index) })))
const mapAnchors = computed(() => anchors.value.flatMap((anchor, index) => validMajorPoint(anchor)
  ? [{ id: anchor.id, latitude: anchor.latitude, longitude: anchor.longitude, label: majorPointLabel(index),
    role: (index === 0 ? 'start' : index === anchors.value.length - 1 ? 'end' : 'middle') as 'start' | 'middle' | 'end' }] : []))
const selectedAnchorChoice = computed(() => anchorChoices.value.find((choice) => choice.id === selectedAnchorId.value) ?? anchorChoices.value[0])
watch(anchors, () => {
  const first = anchors.value[0]
  const last = anchors.value[anchors.value.length - 1]
  if (validMajorPoint(first)) Object.assign(form.start, { latitude: first.latitude, longitude: first.longitude })
  if (validMajorPoint(last)) Object.assign(form.end, { latitude: last.latitude, longitude: last.longitude })
  if (!allAnchorsReady.value) weatherJobId.value = null
  if (anchors.value.length !== 2) comparisonPhase.value = 'waiting'
}, { deep: true, flush: 'sync' })
const preview = ref<Preview | null>(null)
const settingsRoute = ref<Preview | null>(null)
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
type Rc2Status = {
  available: boolean
  platform: string
  message: string
  generation_directory: string
  helper_available: boolean
  script_available: boolean
  busy: boolean
}
type Rc2Target = { name: string; size?: number; type?: string }
type Rc2Job = {
  job_id: string
  status: 'queued' | 'running' | 'succeeded' | 'failed'
  stage: 'queued' | 'generating' | 'uploading' | 'verified' | 'failed'
  message: string
  target_file_name: string
  source_path: string | null
  backup_directory: string
  backup_path: string | null
  source_sha256: string | null
  readback_sha256: string | null
  remote_state: string | null
}
const rc2Status = ref<Rc2Status | null>(null)
const rc2Targets = ref<Rc2Target[]>([])
const rc2DeviceName = ref('')
const rc2WaypointPath = ref('')
const rc2TargetName = ref('')
const rc2Loading = ref(false)
const rc2Submitting = ref(false)
const rc2Error = ref('')
const rc2Job = ref<Rc2Job | null>(null)
const rc2JobRequestFingerprint = ref('')
const rc2JobRunning = computed(() => rc2Submitting.value || rc2Job.value?.status === 'queued' || rc2Job.value?.status === 'running')
const rc2JobMatchesCurrent = computed(() => !!rc2Job.value && !!preview.value && djiModel.value === 'fly'
  && rc2JobRequestFingerprint.value === JSON.stringify(djiKmzRequestBody()))
const rc2CanImport = computed(() => !!preview.value && djiModel.value === 'fly' && !!rc2Status.value?.available
  && !rc2Status.value?.busy && rc2Targets.value.some((target) => target.name === rc2TargetName.value)
  && !rc2JobRunning.value && !rc2Loading.value && !kmzLoading.value && !loading.value)
let rc2JobPollTimer: ReturnType<typeof setTimeout> | null = null
let rc2ConnectionVersion = 0
let rc2Disposed = false
type WaypointHeadingMode = 'followWayline' | 'manually' | 'fixed' | 'smoothTransition' | 'towardPOI'
type WaypointSetting = {
  altitude_m: number
  heading_mode: WaypointHeadingMode
  heading_angle: number
  gimbal_pitch_angle: number
  export_anchor?: boolean
}
const waypointSettings = ref<WaypointSetting[]>([])
const waypointEditorExpanded = ref(false)
const batchBoundaryIds = ref<string[]>([])
const batchHeadingInput = ref('')
const batchPitchInput = ref('')
const batchFeedback = ref<{ message: string; error: boolean } | null>(null)
const batchPanelElement = ref<HTMLElement | null>(null)
const checkingPoint = ref(false)
const locatingPoint = ref<string | null>(null)
const locationFeedback = ref<LocationFeedback | null>(null)
let locationRequestVersion = 0
const error = ref('')
const placeSelectionFeedback = ref<{ message: string; error: boolean } | null>(null)
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
const flightActive = ref(false)
const flightPlaying = ref(false)
const flightProgress = ref(0)
const flightSpeed = ref(1)
const flightCameraPose = ref<CameraPose | null>(null)
let flightFrame: number | null = null
let flightLastFrame: number | null = null
let flightLastCameraUpdate = 0
let flightWasPlayingBeforeSeek = false
let waypointRenderFrame: number | null = null
let waypointRouteDirty = false
let selectedViewFrustum: ReturnType<typeof cameraFrustum> | null = null
const landingSearchRadiusM = ref(35)
const showStartRange = ref(false)
const showEndRange = ref(false)
type ResizeSide = 'left' | 'right'
function defaultColumnWidths() {
  const viewportWidth = typeof window === 'undefined' ? 1440 : window.innerWidth
  return { left: Math.max(260, viewportWidth * 0.2), right: Math.max(280, viewportWidth * 0.3) }
}
const initialColumnWidths = defaultColumnWidths()
const sidebarWidth = ref(initialColumnWidths.left)
const insightsWidth = ref(initialColumnWidths.right)
let columnWidthsCustomized = false
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
const routeKey = computed(() => JSON.stringify([anchors.value.map((anchor) => [anchor.id, anchor.latitude, anchor.longitude]), roadSource.value, form.clearance_m]))
const weatherCoordinates = computed(() => preview.value && previewKey.value === routeKey.value ? preview.value.geometry.coordinates : null)
const generatedPointCount = computed(() => preview.value ? Math.max(0, preview.value.geometry.coordinates.length - (preview.value.anchor_indices?.length ?? anchors.value.length)) : 0)
const batchAnchorRange = computed(() => {
  if (batchBoundaryIds.value.length !== 2) return null
  const positions = batchBoundaryIds.value.map((id) => anchors.value.findIndex((anchor) => anchor.id === id))
  if (positions.some((index) => index < 0) || positions[0] === positions[1]) return null
  return { first: Math.min(...positions), last: Math.max(...positions) }
})
const batchRouteAnchorIndices = computed(() => {
  const route = preview.value
  if (!route || previewKey.value !== routeKey.value || waypointSettings.value.length !== route.geometry.coordinates.length) return null
  const indices = route.anchor_indices ?? (anchors.value.length === 2 ? [0, route.geometry.coordinates.length - 1] : null)
  if (!indices || indices.length !== anchors.value.length) return null
  if (route.anchors && (route.anchors.length !== anchors.value.length
    || route.anchors.some((anchor, index) => anchor.id !== anchors.value[index]?.id))) return null
  if (indices.some((index, position) => !Number.isInteger(index) || index < 0 || index >= route.geometry.coordinates.length
    || (position > 0 && index <= indices[position - 1]))) return null
  return indices
})
const batchWaypointRange = computed(() => {
  const anchorRange = batchAnchorRange.value
  const indices = batchRouteAnchorIndices.value
  if (!anchorRange || !indices) return null
  const start = indices[anchorRange.first]
  const end = indices[anchorRange.last]
  return { start, end, count: end - start + 1,
    from: majorPointLabel(anchorRange.first), to: majorPointLabel(anchorRange.last) }
})
const editorWaypoints = computed(() => {
  if (!waypointEditorExpanded.value) return []
  const indexes = new Set<number>()
  for (let index = 0; index < Math.min(200, waypointSettings.value.length); index++) indexes.add(index)
  if (selectedWaypointIndex.value !== null && selectedWaypointIndex.value < waypointSettings.value.length) indexes.add(selectedWaypointIndex.value)
  return [...indexes].sort((first, second) => first - second).map((index) => ({ index, setting: waypointSettings.value[index]! }))
})
const earthRoute = computed(() => preview.value?.geometry.coordinates.map(([longitude, latitude], index) => [longitude, latitude, waypointSettings.value[index]?.altitude_m ?? form.altitude_m]) ?? null)
const flightTimeline = computed(() => {
  const coordinates = preview.value?.geometry.coordinates
  if (!coordinates || coordinates.length < 2) return null
  try {
    return createFlightTimeline({ coordinates, waypointSettings: waypointSettings.value,
      speedKmh: Number(form.speed_kmh), defaultAltitudeM: Number(form.altitude_m) })
  } catch { return null }
})
const flightSample = computed<FlightSample | null>(() => flightActive.value && flightTimeline.value
  ? sampleFlight(flightTimeline.value, flightProgress.value) : null)
const mapFlightPose = computed(() => flightSample.value ? {
  longitude: flightSample.value.longitude, latitude: flightSample.value.latitude,
  altitude: flightSample.value.altitude, heading: flightSample.value.heading,
  pitch: flightSample.value.pitch, follow: true, playing: flightActive.value,
} : null)
const flightTimeLabel = computed(() => {
  const sample = flightSample.value
  const timeline = flightTimeline.value
  if (!sample || !timeline) return '00:00 / 00:00'
  return `${formatFlightTime(sample.elapsedSeconds)} / ${formatFlightTime(timeline.durationSeconds)}`
})
const flightNodeLabel = computed(() => {
  const sample = flightSample.value
  if (!sample) return '等待播放'
  return sample.segmentFraction < .02 || sample.segmentFraction > .98
    ? `节点 ${sample.waypointIndex + 1}`
    : `节点 ${sample.segmentIndex + 1} → ${sample.segmentIndex + 2}`
})
const selectedCameraPose = computed<CameraPose | null>(() => {
  const index = selectedWaypointIndex.value
  const coordinate = index === null ? null : preview.value?.geometry.coordinates[index]
  if (index === null || !coordinate) return null
  return { longitude: coordinate[0], latitude: coordinate[1], altitude: waypointSettings.value[index]?.altitude_m ?? form.altitude_m,
    heading: waypointHeading(index), pitch: waypointSettings.value[index]?.gimbal_pitch_angle ?? 0 }
})
const currentCameraPose = computed<CameraPose | null>(() => flightActive.value ? flightCameraPose.value : selectedCameraPose.value)
const earthFootprint = computed(() => {
  const index = selectedWaypointIndex.value
  if (!Cesium || !preview.value || index === null || mapEditMode.value !== 'node') return []
  return cameraFrustum(index, Math.max(10, Number(form.altitude_m) || 100)).ground.map((position: any) => {
    const point = Cesium.Cartographic.fromCartesian(position)
    return [Cesium.Math.toDegrees(point.longitude), Cesium.Math.toDegrees(point.latitude), point.height]
  })
})
const flightFootprint = computed(() => {
  const pose = flightCameraPose.value
  if (!flightActive.value || !Cesium || !pose) return []
  return cameraFrustumAt(pose).ground.map((position: any) => {
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
  if (!weatherCoordinates.value) return '先在左侧完成所有主点和飞行参数，系统才能沿路线匹配天气。'
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
  if (anchors.value.length > 2) return '完整路线可检查天气；多段路线候选比较暂不支持'
  if (windowPhase.value === 'pending' || comparisonPhase.value === 'pending') return '正在比较候选时次与道路路线'
  if (windowPhase.value === 'no_covered_candidate' || comparisonPhase.value === 'coverage_incomplete') return '候选天气覆盖不足，暂不能完成比较'
  if (windowPhase.value === 'available' && comparisonPhase.value === 'no_resolvable_weather_difference') return '时次已比较；无可分辨的天气绕行收益'
  if (windowPhase.value === 'available' && comparisonPhase.value === 'candidate_lower_screening') return '时次已比较；存在较低初筛分道路备选'
  if (windowPhase.value === 'available' && ['no_alternative', 'no_material_improvement'].includes(comparisonPhase.value)) return '时次与道路候选已比较；保留道路基线'
  if (windowPhase.value === 'failed' || comparisonPhase.value === 'failed') return '部分比较未完成，请查看接口提示'
  return '候选时次与路线等待当前资料'
})

watch(routeKey, () => {
  clearBatchRange()
  if (preview.value && previewKey.value !== routeKey.value) { preview.value = null; routeAssessment.value = null; drawCesiumRoute(false) }
})
watch(routeAssessment, () => { drawCesiumRoute(false) })
watch(workspacePanel, async () => {
  await nextTick()
  if (workspacePanel.value === 'map') viewer?.resize()
})

function formatFlightTime(seconds: number) {
  const value = Math.max(0, Math.floor(seconds))
  return `${String(Math.floor(value / 60)).padStart(2, '0')}:${String(value % 60).padStart(2, '0')}`
}

function pauseFlight() {
  flightPlaying.value = false
  flightLastFrame = null
  if (flightFrame !== null) cancelAnimationFrame(flightFrame)
  flightFrame = null
}

function stopFlight() {
  pauseFlight()
  flightActive.value = false
  flightProgress.value = 0
  flightCameraPose.value = null
  flightWasPlayingBeforeSeek = false
  syncCesiumFlight()
  if (cameraPreviewViewer) void nextTick(updateCameraPreview)
}

function updateFlightCamera(now = performance.now(), force = false) {
  if (!flightActive.value || (!force && now - flightLastCameraUpdate < 80)) return
  const sample = flightSample.value
  if (!sample) return
  flightLastCameraUpdate = now
  flightCameraPose.value = { longitude: sample.longitude, latitude: sample.latitude,
    altitude: sample.altitude, heading: sample.heading, pitch: sample.pitch }
  if (baseMap.value === 'current') void nextTick(updateCameraPreview)
}

function advanceFlight(now: number) {
  flightFrame = null
  if (!flightPlaying.value) return
  const duration = flightTimeline.value?.durationSeconds ?? 0
  if (duration <= 0) { stopFlight(); return }
  if (flightLastFrame !== null) {
    const elapsed = Math.max(0, Math.min(.25, (now - flightLastFrame) / 1000))
    flightProgress.value = Math.min(1, flightProgress.value + elapsed * flightSpeed.value / duration)
  }
  flightLastFrame = now
  syncCesiumFlight()
  updateFlightCamera(now)
  if (flightProgress.value >= 1) { pauseFlight(); updateFlightCamera(now, true); return }
  flightFrame = requestAnimationFrame(advanceFlight)
}

function playFlight() {
  if (!flightTimeline.value) return
  if (flightProgress.value >= 1) flightProgress.value = 0
  flightActive.value = true
  mapEditMode.value = 'route'
  flightPlaying.value = true
  flightLastFrame = null
  syncCesiumFlight()
  updateFlightCamera(performance.now(), true)
  if (flightFrame === null) flightFrame = requestAnimationFrame(advanceFlight)
}

function toggleFlight() {
  if (flightPlaying.value) pauseFlight()
  else playFlight()
}

function seekFlight(event: Event) {
  if (!flightTimeline.value) return
  flightActive.value = true
  mapEditMode.value = 'route'
  flightProgress.value = Math.max(0, Math.min(1, Number((event.target as HTMLInputElement).value) / 1000))
  if (flightProgress.value >= 1) pauseFlight()
  syncCesiumFlight()
  updateFlightCamera(performance.now(), true)
}

function beginFlightSeek() {
  flightWasPlayingBeforeSeek = flightPlaying.value
  pauseFlight()
}

function endFlightSeek() {
  const resume = flightWasPlayingBeforeSeek
  flightWasPlayingBeforeSeek = false
  if (resume && flightProgress.value < 1) playFlight()
}

watch(preview, (current, previous) => {
  if (current !== previous) stopFlight()
})

async function chooseBaseMap(next: 'current' | 'earth') {
  if (baseMap.value === next) return
  pauseFlight()
  cancelCameraPreviewDrag()
  finishMapCameraDrag()
  if (next === 'earth') earthMapMounted.value = true
  baseMap.value = next
  await nextTick()
  if (viewer) viewer.useDefaultRenderLoop = next === 'current'
  syncCesiumFlight()
  if (cameraPreviewViewer) cameraPreviewViewer.useDefaultRenderLoop = next === 'current' && (flightActive.value || (mapEditMode.value === 'node' && selectedWaypointIndex.value !== null))
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
  syncCesiumFlight()
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

function formatted(point: MajorPoint) {
  if (!validMajorPoint(point)) return '等待设置经纬度'
  return `${Math.abs(point.latitude).toFixed(4)}°${point.latitude < 0 ? 'S' : 'N'}, ${Math.abs(point.longitude).toFixed(4)}°${point.longitude < 0 ? 'W' : 'E'}`
}

function clearLocationFeedback(target: string) {
  if (locationFeedback.value?.target === target) locationFeedback.value = null
}

function addMajorPoint(point?: Point) {
  clearBatchRange()
  const id = `anchor-${nextAnchorNumber++}`
  anchors.value.push({ id, latitude: point?.latitude ?? null, longitude: point?.longitude ?? null })
  selectedAnchorId.value = id
  selectedWaypointIndex.value = null
  settingsPage.value = 'points'
  mapEditMode.value = 'route'
  return id
}

function removeMajorPoint(id: string) {
  if (anchors.value.length <= 2 || loading.value || checkingPoint.value || locatingPoint.value) return
  const index = anchors.value.findIndex((anchor) => anchor.id === id)
  if (index < 0) return
  clearBatchRange()
  anchors.value.splice(index, 1)
  if (selectedAnchorId.value === id) selectedAnchorId.value = anchors.value[Math.min(index, anchors.value.length - 1)].id
  clearLocationFeedback(id)
  selectedWaypointIndex.value = null
}

function moveMajorPoint(id: string, direction: -1 | 1) {
  if (loading.value || checkingPoint.value || locatingPoint.value) return
  const index = anchors.value.findIndex((anchor) => anchor.id === id)
  const target = index + direction
  if (index < 0 || target < 0 || target >= anchors.value.length) return
  clearBatchRange()
  ;[anchors.value[index], anchors.value[target]] = [anchors.value[target], anchors.value[index]]
  selectedWaypointIndex.value = null
}

function useCurrentLocation(target: string) {
  if (loading.value || checkingPoint.value || locatingPoint.value) return
  const anchor = anchors.value.find((candidate) => candidate.id === target)
  if (!anchor) return
  if (!navigator.geolocation) {
    locationFeedback.value = { target, message: '当前页面无法使用定位，请用 HTTPS 或本机地址打开并检查浏览器权限。', error: true }
    return
  }

  const requestVersion = ++locationRequestVersion
  locatingPoint.value = target
  locationFeedback.value = { target, message: '正在获取当前位置…' }
  try {
    navigator.geolocation.getCurrentPosition(({ coords }) => {
      if (requestVersion !== locationRequestVersion) return
      locatingPoint.value = null
      if (loading.value || checkingPoint.value) {
        locationFeedback.value = { target, message: '正在处理其他点位，请稍后重新定位。', error: true }
        return
      }
      if (!Number.isFinite(coords.latitude) || !Number.isFinite(coords.longitude)
        || Math.abs(coords.latitude) > 90 || Math.abs(coords.longitude) > 180) {
        locationFeedback.value = { target, message: '浏览器返回的位置坐标无效，请重试。', error: true }
        return
      }
      const point = { latitude: Number(coords.latitude.toFixed(6)), longitude: Number(coords.longitude.toFixed(6)) }
      locationFeedback.value = { target, message: '已获取当前位置，正在检查点位…' }
      const accuracy = Number.isFinite(coords.accuracy) ? `，定位精度约 ${Math.ceil(coords.accuracy)} 米` : ''
      void chooseMapPoint(point, target).then(() => {
        if (requestVersion !== locationRequestVersion) return
        if (anchor.latitude === point.latitude && anchor.longitude === point.longitude) {
          locationFeedback.value = { target, message: `已填入当前位置${accuracy}，请在地图上核对点位。` }
        } else {
          locationFeedback.value = { target, message: error.value || '当前位置未能设置为此点位。', error: true }
        }
      })
    }, (cause) => {
      if (requestVersion !== locationRequestVersion) return
      locatingPoint.value = null
      const message = cause.code === 1 ? '浏览器未允许读取位置，请在站点设置中开启定位权限。'
        : cause.code === 2 ? '暂时无法确定位置，请检查设备定位服务后重试。'
          : cause.code === 3 ? '获取位置超时，请重试。' : '获取位置失败，请重试。'
      locationFeedback.value = { target, message, error: true }
    }, { enableHighAccuracy: true, maximumAge: 0, timeout: 15_000 })
  } catch {
    locatingPoint.value = null
    locationFeedback.value = { target, message: '当前浏览器无法获取位置，请检查定位权限后重试。', error: true }
  }
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
  const previous = new Map<string, WaypointSetting[]>()
  const previousAnchors = new Map<string, WaypointSetting>()
  settingsRoute.value?.geometry.coordinates.forEach((point, index) => {
    const setting = waypointSettings.value[index]
    if (setting) {
      const key = `${point[0]},${point[1]}`
      previous.set(key, [...(previous.get(key) ?? []), setting])
    }
  })
  settingsRoute.value?.anchors?.forEach((anchor, index) => {
    const coordinateIndex = settingsRoute.value?.anchor_indices?.[index]
    const setting = coordinateIndex === undefined ? undefined : waypointSettings.value[coordinateIndex]
    if (setting) previousAnchors.set(anchor.id, setting)
  })
  const anchorIndexes = new Set(route.anchor_indices ?? [0, route.geometry.coordinates.length - 1])
  const anchorIdsByIndex = new Map((route.anchor_indices ?? []).map((index, anchorIndex) => [index, route.anchors?.[anchorIndex]?.id]))
  waypointSettings.value = route.geometry.coordinates.map(([longitude, latitude], index) => {
    const coordinateSetting = previous.get(`${longitude},${latitude}`)?.shift()
    const prior = previousAnchors.get(anchorIdsByIndex.get(index) ?? '') ?? coordinateSetting
    return {
      ...(prior ?? {
        altitude_m: form.altitude_m,
        heading_mode: 'followWayline',
        heading_angle: 0,
        gimbal_pitch_angle: 0,
      }),
      export_anchor: anchorIndexes.has(index) || prior?.export_anchor === true,
    }
  })
  settingsRoute.value = route
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
  return cameraFrustumAt({ longitude, latitude, altitude: setting?.altitude_m ?? routeAltitude,
    heading: waypointHeading(index), pitch: setting?.gimbal_pitch_angle ?? 0 })
}

function cameraFrustumAt(pose: CameraPose) {
  const { longitude, latitude, altitude } = pose
  const yaw = pose.heading * Math.PI / 180
  const pitch = pose.pitch * Math.PI / 180
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
  const row = (event.target as Element).closest<HTMLElement>('.waypoint-row')
  if (!row) return
  const index = Number(row.dataset.waypointIndex)
  if (index >= 0 && waypointSettings.value[index]) {
    waypointSettings.value[index].export_anchor = true
    scheduleWaypointRender()
  }
}

function clearBatchRange() {
  batchBoundaryIds.value = []
  batchHeadingInput.value = ''
  batchPitchInput.value = ''
  batchFeedback.value = null
}

function toggleBatchBoundary(id: string) {
  if (!batchRouteAnchorIndices.value || loading.value || checkingPoint.value) return
  const current = batchBoundaryIds.value
  batchBoundaryIds.value = current.includes(id) ? current.filter((item) => item !== id)
    : current.length < 2 ? [...current, id] : current
  batchFeedback.value = null
  if (batchBoundaryIds.value.length === 2) void nextTick(() => batchPanelElement.value?.scrollIntoView({ block: 'nearest' }))
}

function applyBatchCamera() {
  const range = batchWaypointRange.value
  if (!range || loading.value || checkingPoint.value || kmzLoading.value) return
  const headingText = batchHeadingInput.value.trim()
  const pitchText = batchPitchInput.value.trim()
  if (!headingText && !pitchText) {
    batchFeedback.value = { message: '请至少填写一个视角参数。', error: true }
    return
  }
  const heading = headingText ? Number(headingText) : null
  const pitch = pitchText ? Number(pitchText) : null
  if (heading !== null && (!Number.isFinite(heading) || heading < 0 || heading > 360)) {
    batchFeedback.value = { message: '机头水平角度须在 0–360° 之间。', error: true }
    return
  }
  if (pitch !== null && (!Number.isFinite(pitch) || pitch < -90 || pitch > 45)) {
    batchFeedback.value = { message: '镜头俯仰角须在 -90–45° 之间。', error: true }
    return
  }
  for (let index = range.start; index <= range.end; index++) {
    const setting = waypointSettings.value[index]
    if (heading !== null) {
      setting.heading_mode = 'fixed'
      setting.heading_angle = heading % 360
    }
    if (pitch !== null) setting.gimbal_pitch_angle = pitch
  }
  batchFeedback.value = { message: `已应用到 ${range.count} 个航点（含 ${range.from} 和 ${range.to}）。`, error: false }
  kmzError.value = ''
  scheduleWaypointRender(true)
}

function onWaypointEditorToggle(event: Event) {
  waypointEditorExpanded.value = (event.target as HTMLDetailsElement).open
}

function selectWaypoint(index: number) {
  if (!preview.value || index < 0 || index >= waypointSettings.value.length) return
  if (flightActive.value) stopFlight()
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
  const pose = currentCameraPose.value
  if (!pose || !Cesium) return
  if (!cameraPreviewViewer) initCameraPreview()
  if (!cameraPreviewViewer) return
  cameraPreviewViewer.camera.setView({
    destination: Cesium.Cartesian3.fromDegrees(pose.longitude, pose.latitude, pose.altitude),
    orientation: { heading: Cesium.Math.toRadians(pose.heading), pitch: Cesium.Math.toRadians(pose.pitch), roll: 0 },
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
  if (event.button !== 0 || !event.isPrimary || cameraPreviewDrag || flightActive.value || mapEditMode.value !== 'node' || index === null || !setting || !element) return
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

watch([mapEditMode, selectedWaypointIndex, baseMap, flightActive], async () => {
  await nextTick()
  if (flightActive.value || (mapEditMode.value === 'node' && selectedWaypointIndex.value !== null)) {
    updateCameraPreview()
    if (cameraPreviewViewer && baseMap.value === 'current') {
      cameraPreviewViewer.useDefaultRenderLoop = true
      cameraPreviewViewer.resize()
      cameraPreviewViewer.scene.requestRender()
    }
  }
  if (cameraPreviewViewer && (baseMap.value === 'earth' || (!flightActive.value && (mapEditMode.value !== 'node' || selectedWaypointIndex.value === null)))) {
    cameraPreviewViewer.useDefaultRenderLoop = false
  }
})

watch(mapEditMode, () => { drawCesiumRoute(false) })

function handleWaypointKeydown(event: KeyboardEvent) {
  const target = event.target as HTMLElement | null
  if (target?.matches('input, select, textarea, [contenteditable="true"]')) return
  if (flightActive.value || mapEditMode.value !== 'node' || selectedWaypointIndex.value === null) return
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
  if (anchors.value.length > 2) return
  clearBatchRange()
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
      body: JSON.stringify(djiKmzRequestBody()),
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

function djiKmzRequestBody() {
  const route = preview.value
  if (!route) throw new Error('请先计算航线。')
  return {
    coordinates: route.geometry.coordinates,
    altitude_m: form.altitude_m,
    speed_kmh: form.speed_kmh,
    drone_model: djiModel.value,
    waypoint_settings: waypointSettings.value,
  }
}

async function rc2ResponseError(response: Response, fallback: string) {
  const payload = await response.json().catch(() => ({}))
  return new Error(typeof payload.detail === 'string' ? payload.detail : fallback)
}

async function refreshRc2Connection() {
  if (rc2Loading.value || rc2JobRunning.value) return
  const version = ++rc2ConnectionVersion
  rc2Loading.value = true
  rc2Error.value = ''
  try {
    const statusResponse = await fetch('/api/dji/rc2/status')
    if (!statusResponse.ok) throw await rc2ResponseError(statusResponse, '无法检查 RC 2 连接状态。')
    const status = await statusResponse.json() as Rc2Status
    if (version !== rc2ConnectionVersion) return
    rc2Status.value = status
    if (!status.available) {
      rc2Targets.value = []
      rc2DeviceName.value = ''
      rc2WaypointPath.value = ''
      rc2TargetName.value = ''
      return
    }
    const targetsResponse = await fetch('/api/dji/rc2/targets')
    if (!targetsResponse.ok) throw await rc2ResponseError(targetsResponse, '无法读取 RC 2 上的占位航线。')
    const result = await targetsResponse.json() as { device_name: string; waypoint_path: string; targets: Rc2Target[] }
    if (version !== rc2ConnectionVersion) return
    rc2DeviceName.value = result.device_name
    rc2WaypointPath.value = result.waypoint_path
    rc2Targets.value = result.targets
    if (!result.targets.some((target) => target.name === rc2TargetName.value)) rc2TargetName.value = ''
  } catch (cause) {
    if (version === rc2ConnectionVersion) {
      if (rc2Status.value) rc2Status.value = { ...rc2Status.value, available: false, message: '无法读取已连接遥控器的占位航线。' }
      rc2Targets.value = []
      rc2TargetName.value = ''
      rc2Error.value = cause instanceof Error ? cause.message : '读取 RC 2 状态失败。'
    }
  } finally {
    if (version === rc2ConnectionVersion) rc2Loading.value = false
  }
}

async function refreshRc2Job() {
  if (rc2Disposed) return
  const jobId = rc2Job.value?.job_id
  if (!jobId) return
  if (rc2JobPollTimer) clearTimeout(rc2JobPollTimer)
  rc2JobPollTimer = null
  try {
    const response = await fetch(`/api/dji/rc2/jobs/${encodeURIComponent(jobId)}`)
    if (!response.ok) throw await rc2ResponseError(response, '无法读取导入任务状态。')
    const job = await response.json() as Rc2Job
    if (rc2Disposed) return
    if (rc2Job.value?.job_id !== jobId) return
    rc2Job.value = job
    rc2Error.value = ''
    if (job.status === 'succeeded' || job.status === 'failed') {
      void refreshRc2Connection()
    } else {
      rc2JobPollTimer = setTimeout(() => void refreshRc2Job(), 1500)
    }
  } catch (cause) {
    if (!rc2Disposed) rc2Error.value = `${cause instanceof Error ? cause.message : '读取导入任务状态失败。'} 可点击“刷新任务状态”重试。`
  }
}

async function importToRc2() {
  if (!rc2CanImport.value) return
  const targetFileName = rc2TargetName.value
  const kmzRequest = djiKmzRequestBody()
  rc2Submitting.value = true
  rc2Error.value = ''
  rc2Job.value = null
  try {
    const response = await fetch('/api/dji/rc2/import', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...kmzRequest, target_file_name: targetFileName }),
    })
    if (!response.ok) throw await rc2ResponseError(response, '无法开始 RC 2 导入任务。')
    const result = await response.json() as { job_id: string; status: 'queued'; message: string; target_file_name: string }
    rc2JobRequestFingerprint.value = JSON.stringify(kmzRequest)
    rc2Job.value = {
      job_id: result.job_id, status: result.status, stage: 'queued', message: result.message,
      target_file_name: result.target_file_name, source_path: null, backup_directory: '', backup_path: null,
      source_sha256: null, readback_sha256: null, remote_state: null,
    }
  } catch (cause) {
    rc2Error.value = cause instanceof Error ? cause.message : '无法开始 RC 2 导入任务。'
  } finally {
    rc2Submitting.value = false
  }
  if (rc2Job.value?.status === 'queued') void refreshRc2Job()
}

watch([preview, djiModel], () => {
  if (preview.value && djiModel.value === 'fly' && !rc2Status.value) void refreshRc2Connection()
})

async function calculate(flyToRoute: boolean | SubmitEvent = true) {
  if (loading.value || locatingPoint.value) return
  if (!allAnchorsReady.value) {
    settingsPage.value = 'points'
    const missingIndex = anchors.value.findIndex((anchor) => !validMajorPoint(anchor))
    error.value = missingIndex >= 0 ? `请先填写主点 ${majorPointLabel(missingIndex)} 的有效经纬度。` : '至少需要两个有效主点。'
    return
  }
  clearBatchRange()
  const controller = new AbortController()
  const requestVersion = ++routeRequestVersion
  const requestedKey = routeKey.value
  routeController = controller
  loading.value = true
  error.value = ''
  kmzError.value = ''
  preview.value = null
  routeAssessment.value = null
  drawCesiumRoute()
  try {
    const response = await fetch('/api/routes/plan-mission', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        anchors: anchors.value.map((anchor) => ({ id: anchor.id, latitude: anchor.latitude, longitude: anchor.longitude })),
        altitude_m: form.altitude_m, speed_kmh: form.speed_kmh, clearance_m: form.clearance_m,
        road_source: roadSource.value,
      }),
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
  return Math.max(minimumWidth(side), window.innerWidth - other - mapMinimum)
}
function resizeViewer() {
  requestAnimationFrame(() => { if (viewer && !viewer.isDestroyed()) viewer.resize() })
}
function setColumnWidth(side: ResizeSide, value: number) {
  columnWidthsCustomized = true
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
  const widths = defaultColumnWidths()
  sidebarWidth.value = widths.left
  insightsWidth.value = widths.right
  columnWidthsCustomized = false
  resizeViewer()
}
function handleColumnViewportResize() {
  if (!columnWidthsCustomized) {
    resetColumnWidths()
    return
  }
  sidebarWidth.value = clamp(sidebarWidth.value, minimumWidth('left'), widthLimit('left'))
  insightsWidth.value = clamp(insightsWidth.value, minimumWidth('right'), widthLimit('right'))
  resizeViewer()
}
function toggleRange(point: 'start' | 'end') {
  if (point === 'start') showStartRange.value = !showStartRange.value
  else showEndRange.value = !showEndRange.value
  drawCesiumRoute(false)
}

function chooseSearchedPoint(selection: { target: string; point: Point }) {
  placeSelectionFeedback.value = { message: '正在检查点位…', error: false }
  void chooseMapPoint(selection.point, selection.target, true)
}

async function chooseMapPoint(point: Point, target: string = selectedAnchorId.value, fromPlaceSearch = false) {
  if (loading.value || checkingPoint.value || locatingPoint.value) {
    if (fromPlaceSearch) placeSelectionFeedback.value = { message: '正在处理其他点位，请稍后重试。', error: true }
    return
  }
  if (!fromPlaceSearch) placeSelectionFeedback.value = null
  const index = target === 'new' ? anchors.value.length : anchors.value.findIndex((anchor) => anchor.id === target)
  if (index < 0) return
  const isLandingPoint = index === 0 || index === anchors.value.length - 1 || target === 'new'
  const label = `${target === 'new' || index === anchors.value.length - 1 ? '终点' : index === 0 ? '起点' : '途经点'} ${majorPointLabel(index)}`
  checkingPoint.value = true
  error.value = ''
  try {
    if (!Number.isFinite(point.latitude) || Math.abs(point.latitude) > 90 || !Number.isFinite(point.longitude) || Math.abs(point.longitude) > 180) {
      throw new Error('点位经纬度无效，请重新选择。')
    }
    if (!isLandingPoint) {
      const anchor = anchors.value[index]
      anchor.latitude = point.latitude
      anchor.longitude = point.longitude
      selectedAnchorId.value = anchor.id
      clearLocationFeedback(anchor.id)
      routeAssessment.value = null
      if (fromPlaceSearch) placeSelectionFeedback.value = { message: `已设置${label}。`, error: false }
      if (allAnchorsReady.value) void calculate(false)
      return
    }
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
      error.value = `${label} 位于已收录建筑或建筑缓冲区，不能作为候选起降点。`
      if (fromPlaceSearch) placeSelectionFeedback.value = { message: error.value, error: true }
      return
    }

    const selectedId = target === 'new' ? addMajorPoint(point) : target
    const selected = anchors.value.find((anchor) => anchor.id === selectedId)
    if (!selected) throw new Error('要设置的主点不存在，请重新选择。')
    selected.latitude = point.latitude
    selected.longitude = point.longitude
    selectedAnchorId.value = selected.id
    clearLocationFeedback(selected.id)
    routeAssessment.value = null
    landingSearchRadiusM.value = inspection.search_radius_m
    if (inspection.state === 'no_road' && roadSource.value === 'local') {
      preview.value = null
      drawCesiumRoute()
      error.value = `${label} 周围 ${inspection.search_radius_m} 米内没有可连接道路。该点仍需现场核验。`
      if (fromPlaceSearch) placeSelectionFeedback.value = { message: `已设置${label}，但周围 ${inspection.search_radius_m} 米内没有可连接道路。该点仍需现场核验。`, error: true }
      return
    }

    // A map click changes the route, but should not reset the camera the user
    // is currently inspecting.
    if (fromPlaceSearch) placeSelectionFeedback.value = { message: `已设置${label}。`, error: false }
    if (allAnchorsReady.value) void calculate(false)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '暂时无法检查该点位。'
    if (fromPlaceSearch) placeSelectionFeedback.value = { message: error.value, error: true }
  } finally {
    checkingPoint.value = false
  }
}

function syncCesiumFlight() {
  if (!viewer || !Cesium) return
  const previous = viewer.entities.getById('flight-drone')
  const sample = flightSample.value
  const visible = baseMap.value === 'current' && mapElement.value?.classList.contains('visible')
  if (!sample || !visible) {
    if (previous) viewer.entities.remove(previous)
    viewer.scene.requestRender()
    return
  }
  const entity = previous ?? viewer.entities.add({
    id: 'flight-drone', name: '模拟无人机位置',
    position: Cesium.Cartesian3.fromDegrees(sample.longitude, sample.latitude, sample.altitude),
    billboard: { image: '/svg/jiantou.svg', width: 38, height: 38,
      verticalOrigin: Cesium.VerticalOrigin.CENTER, horizontalOrigin: Cesium.HorizontalOrigin.CENTER,
      disableDepthTestDistance: Number.POSITIVE_INFINITY,
      color: Cesium.Color.fromCssColorString('#1477bd') },
  })
  entity.position = Cesium.Cartesian3.fromDegrees(sample.longitude, sample.latitude, sample.altitude)
  entity.billboard.rotation = Cesium.Math.toRadians(sample.heading - 90)
  if (mapViewMode.value === '2d') {
    viewer.camera.setView({ destination: Cesium.Cartesian3.fromDegrees(sample.longitude, sample.latitude, 900) })
  } else {
    const rear = offsetCoordinate(sample.longitude, sample.latitude, (sample.heading + 180) % 360, 200)
    viewer.camera.setView({
      destination: Cesium.Cartesian3.fromDegrees(rear[0], rear[1], sample.altitude + 160),
      orientation: { heading: Cesium.Math.toRadians(sample.heading), pitch: Cesium.Math.toRadians(-28), roll: 0 },
    })
  }
  viewer.scene.requestRender()
}

function drawCesiumRoute(flyToRoute = true) {
  const flatView = mapViewMode.value === '2d'
  if (!viewer || !Cesium) return
  selectedViewFrustum = null
  viewer.entities.removeAll()
  viewer.scene.requestRender()
  const visibleAnchors = anchors.value.flatMap((anchor, index) => validMajorPoint(anchor)
    ? [{ ...anchor, index, label: majorPointLabel(index), title: majorPointTitle(index) }] : [])
  const start = visibleAnchors[0]
  const end = visibleAnchors[visibleAnchors.length - 1]
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
  if (showStartRange.value && start?.index === 0) addRange(start, '起点接路搜索范围', '#0f6cbd')
  if (showEndRange.value && end?.index === anchors.value.length - 1) addRange(end, '终点接路搜索范围', '#5b5fc7')
  visibleAnchors.forEach((anchor) => {
    const color = anchor.index === 0 ? '#0f6cbd' : anchor.index === anchors.value.length - 1 ? '#5b5fc7' : '#197b78'
    viewer.entities.add({
      name: `${anchor.title} ${anchor.label}`,
      position: Cesium.Cartesian3.fromDegrees(anchor.longitude, anchor.latitude, 0),
      point: { pixelSize: 15, color: Cesium.Color.fromCssColorString(color), outlineColor: Cesium.Color.WHITE, outlineWidth: 3 },
      label: { text: `${anchor.label}  ${anchor.title}`, font: '600 14px sans-serif', fillColor: Cesium.Color.fromCssColorString('#242424'), outlineColor: Cesium.Color.WHITE, outlineWidth: 3, style: Cesium.LabelStyle.FILL_AND_OUTLINE, pixelOffset: new Cesium.Cartesian2(0, -27) },
    })
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
  const dropStep = Math.max(1, Math.ceil(coordinates.length / (mapEditMode.value === 'node' ? 250 : 120)))
  const waypointIndexes = Array.from(new Set([
    0,
    ...(preview.value.anchor_indices ?? []),
    ...coordinates.map((_, index) => index).filter(index => index % dropStep === 0),
    coordinates.length - 1,
  ]))
  const displayedWaypointIndexes = Array.from(new Set([
    ...waypointIndexes,
    ...(selectedWaypointIndex.value === null ? [] : [selectedWaypointIndex.value]),
  ])).sort((first, second) => first - second)
  const anchorIndexLabels = new Map((preview.value.anchor_indices ?? [0, coordinates.length - 1])
    .map((coordinateIndex, anchorIndex) => [coordinateIndex, majorPointLabel(anchorIndex)] as const))
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
      point: { show: flatView || mapEditMode.value !== 'node' || selected, disableDepthTestDistance: Number.POSITIVE_INFINITY, pixelSize: selected ? 28 : anchorIndexLabels.has(index) ? 14 : 9, color: selected ? Cesium.Color.fromCssColorString('#ffb547') : routeColor, outlineColor: selected ? Cesium.Color.fromCssColorString('#ffd54f') : Cesium.Color.WHITE, outlineWidth: selected ? 4 : 2 },
      label: {
        text: anchorIndexLabels.has(index) ? `${anchorIndexLabels.get(index)} 3D ${index === 0 ? '起点' : index === coordinates.length - 1 ? '终点' : '途经点'}` : `W${String(index + 1).padStart(2, '0')}`,
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
  const centerLongitude = (Math.min(...coordinates.map((point) => point[0])) + Math.max(...coordinates.map((point) => point[0]))) / 2
  const centerLatitude = (Math.min(...coordinates.map((point) => point[1])) + Math.max(...coordinates.map((point) => point[1]))) / 2
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
    // Cesium uses Ctrl+left drag for tilt by default; keep the same rotation shortcut as the visible MapV map.
    const cameraController = viewer.scene.screenSpaceCameraController
    const ctrlLeftDrag = { eventType: Cesium.CameraEventType.LEFT_DRAG, modifier: Cesium.KeyboardEventModifier.CTRL }
    const rotateTypes = cameraController.rotateEventTypes
    cameraController.rotateEventTypes = [...(Array.isArray(rotateTypes) ? rotateTypes : [rotateTypes]), ctrlLeftDrag]
    const tiltTypes = cameraController.tiltEventTypes
    cameraController.tiltEventTypes = (Array.isArray(tiltTypes) ? tiltTypes : [tiltTypes]).filter((gesture: any) =>
      gesture?.eventType !== ctrlLeftDrag.eventType || gesture?.modifier !== ctrlLeftDrag.modifier)
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
    cameraPreviewViewer.imageryLayers.addImageryProvider(new Cesium.OpenStreetMapImageryProvider({
      url: 'https://tile.openstreetmap.org/',
      credit: new Cesium.Credit('<a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">© OpenStreetMap contributors</a>', true),
    }))
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
  window.addEventListener('resize', handleColumnViewportResize)
  handleColumnViewportResize()
  window.addEventListener('keydown', handleWaypointKeydown)
  window.addEventListener('blur', cancelCameraPreviewDrag)
  window.addEventListener('blur', finishMapCameraDrag)
  window.addEventListener('blur', pauseFlight)
  window.addEventListener('pointerup', endFlightSeek)
  window.addEventListener('pointercancel', endFlightSeek)
  window.addEventListener('pointerup', finishMapCameraDrag)
  scheduleDepartureClock()
  void calculate()
  if ((window as any).Cesium) {
    initMap()
  }
  else window.addEventListener('load', initMap, { once: true })
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', handleColumnViewportResize)
  rc2Disposed = true
  rc2ConnectionVersion++
  if (rc2JobPollTimer) clearTimeout(rc2JobPollTimer)
  locationRequestVersion++
  window.removeEventListener('keydown', handleWaypointKeydown)
  window.removeEventListener('blur', cancelCameraPreviewDrag)
  window.removeEventListener('blur', finishMapCameraDrag)
  window.removeEventListener('blur', pauseFlight)
  window.removeEventListener('pointerup', endFlightSeek)
  window.removeEventListener('pointercancel', endFlightSeek)
  window.removeEventListener('pointerup', finishMapCameraDrag)
  cancelCameraPreviewDrag()
  finishMapCameraDrag()
  pauseFlight()
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
        <div class="section-heading"><span class="eyebrow"><span class="svg-icon icon-route" aria-hidden="true"></span>航线设置</span><h1>规划一次飞行</h1><PlaceSearch :disabled="loading || checkingPoint" :targets="anchorChoices" :selection-feedback="placeSelectionFeedback" @choose-point="chooseSearchedPoint" @clear-selection-feedback="placeSelectionFeedback = null" /></div>

        <form class="route-form" @submit.prevent="calculate" @invalid.capture="revealInvalidSetting">
          <nav class="settings-pages" aria-label="航线设置分组"><button type="button" :aria-pressed="settingsPage === 'points'" @click="settingsPage = 'points'">主点与坐标</button><button type="button" :aria-pressed="settingsPage === 'mission'" @click="settingsPage = 'mission'">飞行参数</button></nav>
          <div class="settings-fields">
          <div class="settings-group" data-settings-page="points" :class="{ 'settings-active': settingsPage === 'points' }">
          <p class="batch-range-guide">{{ batchRouteAnchorIndices ? batchBoundaryIds.length === 1 ? '再勾选一个主点，确定批量视角区间。' : '勾选两个主点，统一设置两端及中间航点的视角。' : '计算道路路线后可勾选两个主点，批量设置视角。' }}</p>
          <div v-for="(anchor, index) in anchors" :key="anchor.id" class="major-point-card" :class="{ 'is-selected': selectedAnchorId === anchor.id, 'is-batch-boundary': batchBoundaryIds.includes(anchor.id), 'is-batch-range': batchAnchorRange && index >= batchAnchorRange.first && index <= batchAnchorRange.last }">
            <div class="point-heading"><input class="batch-boundary-checkbox" type="checkbox" :checked="batchBoundaryIds.includes(anchor.id)" :disabled="!batchRouteAnchorIndices || loading || checkingPoint || (batchBoundaryIds.length === 2 && !batchBoundaryIds.includes(anchor.id))" :aria-label="`选择 ${majorPointLabel(index)} 作为批量视角区间边界`" @change="toggleBatchBoundary(anchor.id)" /><span class="point-mark" :class="index === 0 ? 'point-a' : index === anchors.length - 1 ? 'point-b' : 'point-middle'">{{ majorPointLabel(index) }}</span><div><div class="point-title-row"><strong>{{ majorPointTitle(index) }}</strong><button type="button" class="point-location-button" :disabled="loading || checkingPoint || locatingPoint !== null" :aria-label="locatingPoint === anchor.id ? `正在定位${majorPointTitle(index)} ${majorPointLabel(index)}` : `使用当前位置设置${majorPointTitle(index)} ${majorPointLabel(index)}`" :title="`使用当前位置设置${majorPointTitle(index)} ${majorPointLabel(index)}`" @click="useCurrentLocation(anchor.id)"><span class="svg-icon icon-location-on" aria-hidden="true"></span></button></div><small>{{ formatted(anchor) }}</small></div><button v-if="index === 0 || index === anchors.length - 1" type="button" class="range-toggle" :class="{ active: index === 0 ? showStartRange : showEndRange }" :aria-pressed="index === 0 ? showStartRange : showEndRange" :disabled="loading || checkingPoint" title="显示或隐藏接路搜索范围" @click="toggleRange(index === 0 ? 'start' : 'end')"><span class="svg-icon icon-eye" aria-hidden="true"></span><span>范围：{{ (index === 0 ? showStartRange : showEndRange) ? '开' : '关' }}</span></button></div>
            <div class="coordinate-row"><label>纬度<input v-model.number="anchor.latitude" type="number" step="0.000001" min="-90" max="90" required :disabled="loading" @input="clearLocationFeedback(anchor.id)" /></label><label>经度<input v-model.number="anchor.longitude" type="number" step="0.000001" min="-180" max="180" required :disabled="loading" @input="clearLocationFeedback(anchor.id)" /></label></div>
            <p v-if="locationFeedback?.target === anchor.id" class="point-location-feedback" :class="{ 'is-error': locationFeedback.error }" :role="locationFeedback.error ? 'alert' : 'status'">{{ locationFeedback.message }}</p>
            <div class="major-point-actions"><button type="button" :class="{ active: selectedAnchorId === anchor.id }" :aria-pressed="selectedAnchorId === anchor.id" :disabled="checkingPoint || loading" @click="selectedAnchorId = anchor.id; mapEditMode = 'route'">地图选此点</button><button type="button" :disabled="index === 0 || loading || checkingPoint" :aria-label="`上移主点 ${majorPointLabel(index)}`" @click="moveMajorPoint(anchor.id, -1)">上移</button><button type="button" :disabled="index === anchors.length - 1 || loading || checkingPoint" :aria-label="`下移主点 ${majorPointLabel(index)}`" @click="moveMajorPoint(anchor.id, 1)">下移</button><button type="button" :disabled="anchors.length <= 2 || loading || checkingPoint" :aria-label="`删除主点 ${majorPointLabel(index)}`" @click="removeMajorPoint(anchor.id)">删除</button></div>
          </div>
          <button type="button" class="major-point-add" :disabled="loading || checkingPoint || locatingPoint !== null" @click="addMajorPoint()">＋ 添加主点</button>
          <div v-if="batchAnchorRange" ref="batchPanelElement" class="batch-camera-panel" aria-label="批量视角设置">
            <strong>{{ majorPointLabel(batchAnchorRange.first) }}–{{ majorPointLabel(batchAnchorRange.last) }} 批量视角</strong>
            <small v-if="batchWaypointRange">含两个边界点及中间自动生成的点，共 {{ batchWaypointRange.count }} 个航点。</small>
            <div class="batch-camera-fields"><label>机头水平角度 · °<input v-model="batchHeadingInput" type="text" inputmode="decimal" placeholder="留空保持原值" @input="batchFeedback = null" /></label><label>镜头俯仰角 · °<input v-model="batchPitchInput" type="text" inputmode="decimal" placeholder="留空保持原值" @input="batchFeedback = null" /></label></div>
            <button type="button" :disabled="!batchWaypointRange || loading || checkingPoint || kmzLoading" @click="applyBatchCamera">应用到区间</button>
            <p v-if="batchFeedback" :class="{ 'is-error': batchFeedback.error }" :role="batchFeedback.error ? 'alert' : 'status'">{{ batchFeedback.message }}</p>
          </div>
          </div>

          <div class="settings-group" data-settings-page="mission" :class="{ 'settings-active': settingsPage === 'mission' }">
          <div class="form-line"></div>
          <div class="coordinate-row"><label>离地高度 AGL · 米<input v-model.number="form.altitude_m" type="number" min="0" max="1000" required :disabled="loading" /></label><label>巡航速度 · km/h<input v-model.number="form.speed_kmh" type="number" min="1" max="200" step="0.5" required :disabled="loading" /></label></div>
          <small class="form-scope-note">目前高度风覆盖 10–100 米；范围外会标记缺测。</small>
          <label class="departure-field">出发时刻 · 北京时间<input v-model="departureBeijing" type="datetime-local" :min="latestDepartureBeijing" required @input="markDepartureManual" /><small>{{ departureAutoManaged ? '默认跟随当前北京时间，每分钟更新一次。' : '已使用手动选择的出发时间。' }}沿线 ETA 按填写速度估算，不含风对地速的影响。</small></label>
          <div class="clearance-row"><label>建筑缓冲 · 米（演示）<input v-model.number="form.clearance_m" type="number" min="0" max="20" step="0.5" required :disabled="loading || roadSource === 'online'" /></label><span>{{ roadSource === 'local' ? '用于排除靠近建筑的道路段' : '在线机动车路线不使用此参数' }}</span></div>
          </div>
          </div>
           <fluent-button class="primary-button" appearance="primary" type="submit" :disabled="loading || locatingPoint !== null"><span class="svg-icon" :class="loading ? 'icon-refresh is-spinning' : 'icon-route'" aria-hidden="true"></span><span>{{ loading ? '计算中…' : '计算道路路线' }}</span></fluent-button>
          <p v-if="error" class="form-error" role="alert">{{ error }}</p>
        </form>
      </aside>

      <section id="route-map" class="map-panel" :class="{ 'panel-active': workspacePanel === 'map' }" aria-label="航线地图">
        <div ref="mapElement" class="cesium-map" :class="{ visible: mapReady && baseMap === 'current' }"></div>
        <EarthMap v-if="earthMapMounted" ref="earthMapRef" :active="baseMap === 'earth'" :view-mode="mapViewMode" :start="form.start" :end="form.end" :anchors="mapAnchors" :route="earthRoute" :selected-index="selectedWaypointIndex" :edit-mode="mapEditMode" :footprint="earthFootprint" :flight-pose="mapFlightPose" :flight-footprint="flightFootprint" :weather-segments="routeAssessment?.weather_risk.segments ?? []" :show-start-range="showStartRange" :show-end-range="showEndRange" :range-meters="landingSearchRadiusM" @select-waypoint="selectWaypoint" @choose-point="chooseMapPoint" @rotate-waypoint="setSelectedWaypointOrientation($event, undefined, true)" />
        <div v-if="!mapReady && baseMap === 'current'" class="map-fallback">
          <div class="grid-lines"></div>
          <p>地图底图加载中</p><small>道路路线按所选数据源计算</small>
        </div>
         <div class="map-top"><div class="map-title-group"><span class="map-badge"><span class="svg-icon icon-layers" aria-hidden="true"></span>{{ (baseMap === 'earth' ? '地球模式' : '当前底图') + (mapViewMode === '3d' ? ' · 3D 航线' : ' · 2D 平面') }}</span><span class="map-mode-chip">{{ form.altitude_m }} m AGL</span><div class="map-source-switch base-map-switch" aria-label="底图"><button type="button" :class="{ active: baseMap === 'current' }" :aria-pressed="baseMap === 'current'" @click="chooseBaseMap('current')">当前底图</button><button type="button" :class="{ active: baseMap === 'earth' }" :aria-pressed="baseMap === 'earth'" @click="chooseBaseMap('earth')">地球模式</button></div><div class="map-source-switch" aria-label="路线数据源"><button type="button" :class="{ active: roadSource === 'local' }" :aria-pressed="roadSource === 'local'" :disabled="loading || checkingPoint" @click="chooseRoadSource('local')">本地</button><button type="button" :class="{ active: roadSource === 'online' }" :aria-pressed="roadSource === 'online'" :disabled="loading || checkingPoint" @click="chooseRoadSource('online')">在线</button></div><div class="map-edit-switch" aria-label="编辑对象"><button type="button" :class="{ active: mapEditMode === 'route' }" :aria-pressed="mapEditMode === 'route'" @click="mapEditMode = 'route'">路径 / 主点</button><button type="button" :class="{ active: mapEditMode === 'node' }" :aria-pressed="mapEditMode === 'node'" @click="mapEditMode = 'node'">节点 / 视角</button></div></div>
          <div class="flight-controls" aria-label="模拟飞行回放">
            <button class="flight-toggle" type="button" :disabled="!flightTimeline" :aria-label="flightPlaying ? '暂停模拟飞行' : flightProgress >= 1 ? '重新播放模拟飞行' : flightActive ? '继续模拟飞行' : '开始模拟飞行'" :aria-pressed="flightPlaying" :title="flightPlaying ? '暂停' : flightProgress >= 1 ? '重播' : flightActive ? '继续' : '模拟飞行'" @click="toggleFlight"><span class="svg-icon icon-drone" aria-hidden="true"></span></button>
            <label class="flight-speed">倍率<select v-model.number="flightSpeed" aria-label="模拟飞行倍率" :disabled="!flightTimeline"><option :value="0.5">0.5×</option><option :value="1">1×</option><option :value="2">2×</option><option :value="4">4×</option><option :value="8">8×</option></select></label>
            <label class="flight-progress-wrap"><span class="sr-only">模拟飞行进度</span><input class="flight-progress" type="range" min="0" max="1000" step="1" :value="Math.round(flightProgress * 1000)" :disabled="!flightTimeline" :aria-valuetext="`${Math.round(flightProgress * 100)}%，${flightTimeLabel}`" :title="flightTimeLabel" @pointerdown="beginFlightSeek" @pointerup="endFlightSeek" @pointercancel="endFlightSeek" @input="seekFlight" /></label>
            <span class="flight-percent">{{ Math.round(flightProgress * 100) }}%</span>
          </div>
          <span v-if="!flightTimeline" class="map-caption">{{ checkingPoint ? '正在检查点位…' : mapEditMode === 'node' ? '点击节点选中，拖动橙色镜头调整朝向' : `地图点击可设置 ${selectedAnchorChoice?.title} ${selectedAnchorChoice?.label}` }}</span></div>
        <div class="map-view-switch map-source-switch view-mode-switch" aria-label="地图视图"><button type="button" :class="{ active: mapViewMode === '2d' }" :aria-pressed="mapViewMode === '2d'" @click="chooseMapViewMode('2d')">2D 平面</button><button type="button" :class="{ active: mapViewMode === '3d' }" :aria-pressed="mapViewMode === '3d'" @click="chooseMapViewMode('3d')">3D 视角</button></div>
        <div v-if="error" class="map-alert" role="alert">{{ error }}</div>
         <div class="map-bottom"><label class="map-anchor-picker"><span class="svg-icon icon-map-pin" aria-hidden="true"></span>地图选点 <select v-model="selectedAnchorId" :disabled="checkingPoint || mapEditMode !== 'route'"><option v-for="choice in anchorChoices" :key="choice.id" :value="choice.id">{{ choice.label }} · {{ choice.title }}</option></select></label><span class="map-radius"><span class="svg-icon icon-map-pin" aria-hidden="true"></span>接路判定 {{ landingSearchRadiusM }} m</span><span class="map-scale"><span class="svg-icon icon-wind" aria-hidden="true"></span>任务高度 {{ form.altitude_m }} m</span></div>
        <div v-if="!flightActive && mapEditMode === 'node' && selectedWaypointIndex !== null && selectedWaypointSetting()" class="node-controller" aria-label="航点控制器"><div class="node-controller-head"><strong>节点 {{ selectedWaypointIndex + 1 }} · 视角控制</strong><span>{{ Math.round(waypointHeading(selectedWaypointIndex)) }}° · {{ selectedWaypointSetting()?.altitude_m ?? form.altitude_m }}m</span></div><div class="node-controller-body"><div class="wasd-pad" aria-label="WASD 平面移动控制"><button type="button" @click="moveSelectedWaypoint(0, 1)">W</button><button type="button" @click="moveSelectedWaypoint(-1, 0)">A</button><button type="button" @click="moveSelectedWaypoint(0, -1)">S</button><button type="button" @click="moveSelectedWaypoint(1, 0)">D</button></div><div class="node-dial"><button type="button" aria-label="逆时针旋转" @click="rotateSelectedWaypoint(-15)">↺</button><strong>{{ Math.round(waypointHeading(selectedWaypointIndex)) }}°</strong><button type="button" aria-label="顺时针旋转" @click="rotateSelectedWaypoint(15)">↻</button></div><div class="node-axis"><button type="button" @click="rotateSelectedWaypoint(0, 5)">镜头上</button><button type="button" @click="rotateSelectedWaypoint(0, -5)">镜头下</button><button type="button" @click="adjustSelectedWaypoint({ altitude_m: Math.min(1500, (selectedWaypointSetting()?.altitude_m ?? form.altitude_m) + 5) })">高度 +</button><button type="button" @click="adjustSelectedWaypoint({ altitude_m: Math.max(1, (selectedWaypointSetting()?.altitude_m ?? form.altitude_m) - 5) })">高度 −</button></div></div><small>W/S 平面前后 ±1m，A/D 平面左右 ±1m，Q/E 航向 ±5°，Z/C 俯仰 ±5°可直接调整水平朝向。</small></div>
        <div v-if="routeAssessment" class="weather-map-legend">沿线初筛：蓝 · 未触发，橙 · 关注，红 · 较强，灰 · 缺测<br />基于 0.25° 场，不是街道级风险，也不代表飞行许可。</div>
        <div class="camera-preview" :class="{ visible: (flightActive || (mapEditMode === 'node' && selectedWaypointIndex !== null)) && (baseMap === 'earth' || cameraPreviewReady) }" aria-label="当前节点相机视角预览"><div ref="cameraPreviewElement" class="camera-preview-map" :data-base-map="baseMap" @pointerdown.capture="handleCameraPreviewPointerDown" @pointermove="handleCameraPreviewPointerMove" @pointerup="handleCameraPreviewPointerEnd" @pointercancel="handleCameraPreviewPointerEnd" @lostpointercapture="handleCameraPreviewPointerEnd"><div ref="cesiumCameraElement" :style="{ position: 'absolute', inset: '0', visibility: baseMap === 'current' ? 'visible' : 'hidden' }"></div><BaiduCameraPreview v-if="baseMap === 'earth' && (flightActive || (mapEditMode === 'node' && selectedWaypointIndex !== null))" :active="baseMap === 'earth' && (flightActive || (mapEditMode === 'node' && selectedWaypointIndex !== null))" :pose="currentCameraPose" /></div><div class="camera-preview-head"><strong>当前视角</strong><span v-if="flightActive">{{ flightNodeLabel }} / {{ Math.round(flightSample?.pitch ?? 0) }}&deg;</span><span v-else-if="selectedWaypointIndex !== null">节点 {{ selectedWaypointIndex + 1 }} · {{ Math.round(selectedWaypointSetting()?.gimbal_pitch_angle ?? 0) }}°</span></div><small>地图模拟视角 · 不是实时相机视频</small></div>
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
            <WeatherPanels v-if="allAnchorsReady" :altitude-m="form.altitude_m" :start="form.start" :end="form.end" @job-ready="weatherJobId = $event" @state-changed="weatherState = $event" />
            <RouteWeatherPanel :job-id="weatherJobId" :coordinates="weatherCoordinates" :departure-beijing="departureBeijing" :speed-kmh="form.speed_kmh" :altitude-m="form.altitude_m" :limits="activeLimits" @assessed="routeAssessment = $event" @phase="routePhase = $event" />
          </div>
        </details>
         <details class="overview-group"><summary><span><span class="svg-icon icon-route" aria-hidden="true"></span>调整出发时间或路线</span><small>可选参考，不会自动替换当前设置</small></summary><div class="overview-group-body overview-planning"><WeatherWindowsPanel :job-id="weatherJobId" :coordinates="weatherCoordinates" :departure-beijing="departureBeijing" :speed-kmh="form.speed_kmh" :altitude-m="form.altitude_m" :limits="activeLimits" @choose="chooseDeparture" @state="windowPhase = $event" /><RoadWeatherComparison v-if="anchors.length === 2" :job-id="weatherJobId" :can-compare="!!weatherCoordinates" :current-coordinates="weatherCoordinates" :start="form.start" :end="form.end" :road-source="roadSource" :clearance-m="form.clearance_m" :departure-beijing="departureBeijing" :speed-kmh="form.speed_kmh" :altitude-m="form.altitude_m" :limits="activeLimits" @choose="chooseRoad" @state="comparisonPhase = $event" /><p v-else class="multi-route-comparison-note">多段路线候选比较暂不支持；沿线天气检查仍按完整路线进行。</p></div></details>
         <details class="overview-group"><summary><span><span class="svg-icon icon-layers" aria-hidden="true"></span>天气规则和技术状态</span><small>仅在需要核对时查看</small></summary><div class="overview-group-body"><details class="limits-settings"><summary>天气初筛阈值</summary><label class="limits-toggle"><input v-model="customLimitsEnabled" type="checkbox" /> 填写自定义气象阈值</label><p>关闭时使用演示阈值。阈值用于天气提示与比较，参考分不是事故概率。</p><template v-if="customLimitsEnabled"><label>阈值名称<input v-model="customLimits.name" maxlength="80" /></label><div class="coordinate-row"><label>最大高度风 · m/s<input v-model.number="customLimits.max_wind_m_s" type="number" min="0.1" max="200" step="0.1" required /></label><label>最大侧风 · m/s<input v-model.number="customLimits.max_crosswind_m_s" type="number" min="0.1" max="200" step="0.1" required /></label></div><div class="coordinate-row"><label>最大逆风 · m/s<input v-model.number="customLimits.max_headwind_m_s" type="number" min="0.1" max="200" step="0.1" required /></label><label>最大地面阵风 · m/s<input v-model.number="customLimits.max_gust_m_s" type="number" min="0.1" max="200" step="0.1" required /></label></div><div class="coordinate-row"><label>最大雨量 · mm/6h<input v-model.number="customLimits.max_rain_6h_mm" type="number" min="0.1" max="500" step="0.1" required /></label><label>最低地面能见度 · m<input v-model.number="customLimits.min_visibility_m" type="number" min="1" max="100000" step="1" required /></label></div></template></details><div class="pipeline"><h3>系统状态</h3><div><span>01</span><p>{{ alignmentProgress }}</p></div><div><span>02</span><p>{{ routeAssessment ? decisionTitle : '沿线初筛等待资料' }}</p></div><div><span>03</span><p>{{ planningProgress }}</p></div></div></div></details>
         <div v-if="preview" class="road-summary"><strong><span class="svg-icon icon-route" aria-hidden="true"></span>当前路线：{{ preview.road_names.join(' → ') || '未命名道路' }}</strong><span>{{ distance }} · 预计用时 {{ elapsed }}</span><details><summary><span class="svg-icon icon-eye" aria-hidden="true"></span>查看路线数据</summary><small>直线 {{ preview.straight_distance_km.toFixed(2) }} km · {{ preview.building_clearance_m === null ? '在线模式未检查建筑缓冲' : `建筑缓冲 ${preview.building_clearance_m} m` }}</small><small>首末主点到道路分别约 {{ preview.start_snap_m }} m / {{ preview.end_snap_m }} m</small><small>手动主点 {{ anchors.length }} 个 · 自动生成小点 {{ generatedPointCount }} 个 · {{ preview.segments?.length ?? 1 }} 段</small><small v-for="segment in preview.segments" :key="segment.id">{{ majorPointLabel(segment.index) }} → {{ majorPointLabel(segment.index + 1) }}：{{ segment.distance_km.toFixed(2) }} km</small><small>{{ preview.data_source }}</small><small>路线数据 © <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap contributors</a></small></details><div class="route-export"><label>DJI 机型模板<select v-model="djiModel"><option value="fly">DJI Fly · Air 3 / Air 3S / Mini 4 Pro</option><option value="m30">DJI Pilot 2 · M30 / M30T</option><option value="m3e">DJI Pilot 2 · Mavic 3E / 3T / 3M</option></select></label><details class="waypoint-editor" @toggle="onWaypointEditorToggle"><summary>逐点设置高度和面向（{{ waypointSettings.length }} 个点）</summary><p class="waypoint-editor-note">高度为相对起飞点高度；机头/镜头水平朝向以真北为 0°，摄像头俯仰角 0° 为水平，负数向下。Air 3/Air 3S 的镜头水平偏航是否独立于机头由 DJI Fly 和机型能力决定。</p><p v-if="waypointSettings.length > 200" class="waypoint-editor-note">此处列出前 200 个点和当前选中点；完整路线与导出仍保留全部 {{ waypointSettings.length }} 个点。</p><div v-for="{ setting, index } in editorWaypoints" :key="index" class="waypoint-row" :data-waypoint-index="index"><strong>点 {{ index + 1 }}</strong><label>高度（m）<input v-model.number="setting.altitude_m" type="number" min="1" max="1500" step="1" /></label><label>机头方向<select v-model="setting.heading_mode"><option value="followWayline">沿航线</option><option value="fixed">固定角度</option><option value="smoothTransition">平滑转向</option><option value="manually">手动控制</option></select></label><label v-if="setting.heading_mode === 'fixed' || setting.heading_mode === 'smoothTransition'">角度（°）<input v-model.number="setting.heading_angle" type="number" step="1" /></label><label>摄像头俯仰（°）<input v-model.number="setting.gimbal_pitch_angle" type="number" min="-90" max="45" step="1" /></label></div></details><button type="button" class="weather-refresh" :disabled="kmzLoading || loading" @click="exportDjiKmz"><span class="svg-icon icon-download" aria-hidden="true"></span>{{ kmzLoading ? '正在生成 KMZ…' : '导出 DJI KMZ 草稿' }}</button><small>按相对起飞点高度生成。路线折点会先抽稀，减少 DJI 中高度标签和航点标记重叠。{{ roadSource === 'online' ? '当前是在线机动车道路参考，不应直接作为飞行线。' : '当前是道路走廊参考，不代表已核实飞行航线。' }}{{ djiModel === 'fly' ? 'DJI Fly 需先创建一条占位航线，再用相同 UUID 文件名替换 waypoint 目录中的 KMZ。' : '导入 DJI Pilot 2 后仍需核对机型、动作、返航、空域和现场条件。' }}文件不是放飞许可。</small><small v-if="djiModel === 'fly'">Air 3/Air 3S 使用 DJI Fly 消费级 WPML；当前通用机型枚举按 RC 2 航线格式生成，若设备拒绝文件需要用设备原生占位 KMZ 校验。</small><small v-if="kmzError" class="form-error" role="alert">{{ kmzError }}</small></div></div>
        <section v-if="(preview && djiModel === 'fly') || rc2Job" class="rc2-import-panel" aria-label="导入 DJI RC 2">
          <div class="rc2-import-heading">
            <h3>导入已连接的 DJI RC 2</h3>
            <button type="button" :disabled="rc2Loading || rc2JobRunning" @click="refreshRc2Connection">{{ rc2Loading ? '检测中…' : '检测 / 刷新设备' }}</button>
          </div>
          <p role="status">{{ rc2Loading ? '正在检查本机和遥控器…' : rc2Status?.message ?? '等待检查 RC 2 连接状态。' }}</p>
          <small v-if="rc2Status?.generation_directory">本机 KMZ 生成目录：{{ rc2Status.generation_directory }}</small>
          <template v-if="preview && djiModel === 'fly' && rc2Status?.available">
            <small v-if="rc2DeviceName">遥控器：{{ rc2DeviceName }}</small>
            <small v-if="rc2WaypointPath">占位航线目录：{{ rc2WaypointPath }}</small>
            <label>选择已有占位航线
              <select v-model="rc2TargetName" :disabled="rc2Loading || rc2JobRunning || !rc2Targets.length">
                <option value="">请选择遥控器上的 .kmz 文件</option>
                <option v-for="target in rc2Targets" :key="target.name" :value="target.name">{{ target.name }}</option>
              </select>
            </label>
            <small v-if="!rc2Targets.length && !rc2Loading">未找到占位航线。请先在 DJI Fly 中创建并保存一条航线，然后刷新设备。</small>
            <button type="button" class="rc2-import-action" :disabled="!rc2CanImport" @click="importToRc2">{{ rc2Submitting ? '正在创建导入任务…' : rc2JobRunning ? '正在导入…' : '生成 KMZ 并替换所选占位航线' }}</button>
            <small v-if="rc2TargetName">将替换 {{ rc2TargetName }}；原文件会先备份，写入后回读校验。</small>
          </template>
          <small v-else-if="preview && djiModel === 'fly'">请用 USB 连接 RC 2，并先在 DJI Fly 中创建一条占位航线；连接后点击“检测 / 刷新设备”。</small>
          <div v-if="rc2Job" class="rc2-job" :class="{ 'rc2-job-failed': rc2Job.status === 'failed', 'rc2-job-done': rc2Job.status === 'succeeded' }" role="status" aria-live="polite">
            <strong>{{ !rc2JobMatchesCurrent ? '上次航线的' : '' }}{{ rc2Job.status === 'succeeded' ? '导入并回读校验完成' : rc2Job.status === 'failed' ? '导入失败' : '导入进行中' }}</strong>
            <small v-if="!rc2JobMatchesCurrent">当前路线或航点设置与提交任务时不同；以下结果对应提交时的版本。</small>
            <span>目标：{{ rc2Job.target_file_name }}</span>
            <ol class="rc2-job-steps">
              <li :class="{ active: rc2Job.stage === 'generating', done: !!rc2Job.source_path }">生成 KMZ</li>
              <li :class="{ active: rc2Job.stage === 'uploading', done: rc2Job.stage === 'verified' }">备份并传输</li>
              <li :class="{ active: rc2Job.stage === 'verified', done: rc2Job.status === 'succeeded' }">回读校验</li>
            </ol>
            <span>{{ rc2Job.message }}</span>
            <small v-if="rc2Job.source_path">生成文件：{{ rc2Job.source_path }}</small>
            <small v-if="rc2Job.backup_directory">备份目录：{{ rc2Job.backup_directory }}</small>
            <small v-if="rc2Job.backup_path">原文件备份：{{ rc2Job.backup_path }}</small>
            <button v-if="rc2JobRunning" type="button" class="rc2-job-refresh" @click="refreshRc2Job">刷新任务状态</button>
          </div>
          <p v-if="rc2Error" class="form-error" role="alert">{{ rc2Error }}</p>
          <small>此操作只适用于 DJI Fly / RC 2。导入完成后仍需在遥控器上打开航线，核对航点与动作。</small>
        </section>
      </aside>
    </main>
  </div>
</template>

<style scoped>
.route-form .settings-fields { overflow-y: auto; overscroll-behavior: contain; padding-right: 5px; }
.batch-range-guide { margin: 2px 4px 6px; color: var(--ui-muted); font-size: var(--font-caption); line-height: 1.4; }
.major-point-card { container-type: inline-size; padding: 12px 4px; border-bottom: 1px solid var(--ui-line); }
.major-point-card.is-batch-range { background: #f0faf9; }
.major-point-card.is-batch-boundary { box-shadow: inset 3px 0 #168782; }
.major-point-card.is-selected { margin-left: -7px; padding-left: 8px; border-left: 3px solid var(--ui-accent); background: #f8fbff; }
.major-point-card .point-heading { margin-bottom: 8px; }
.batch-boundary-checkbox { flex: none; width: 20px; height: 20px; margin: 0; accent-color: #168782; cursor: pointer; }
.batch-boundary-checkbox:disabled { cursor: default; opacity: .5; }
.major-point-card .point-mark { width: 28px; flex: none; }
.major-point-card .point-middle, .major-point-card .point-middle + div .point-location-button { color: #197b78; }
.major-point-actions { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 8px; }
.major-point-actions button { min-height: 26px; padding: 3px 6px; border: 1px solid #dbe2ea; border-radius: 6px; background: #fff; color: #526273; font-size: 11px; cursor: pointer; }
.major-point-actions button.active { border-color: #88bcea; background: #e8f3fc; color: #0f6cbd; font-weight: 700; }
.major-point-actions button:hover:not(:disabled) { border-color: #88bcea; color: #0f6cbd; }
.major-point-actions button:disabled { opacity: .45; cursor: default; }
.major-point-add { display: block; width: 100%; min-height: 38px; margin-top: 12px; border: 1px dashed #83b5df; border-radius: 8px; background: #f2f8fe; color: #0f6cbd; font-size: 13px; font-weight: 700; cursor: pointer; }
.major-point-add:hover:not(:disabled) { background: #e6f1fb; }
.major-point-add:disabled { opacity: .5; cursor: default; }
.batch-camera-panel { margin-top: 12px; padding: 12px; border: 1px solid #b8dedb; border-radius: 8px; background: #f7fffe; box-shadow: 0 4px 16px #172b4d1c; }
.batch-camera-panel > strong, .batch-camera-panel > small { display: block; }
.batch-camera-panel > strong { color: #0b625f; font-size: var(--font-body); }
.batch-camera-panel > small { margin-top: 3px; color: var(--ui-muted); font-size: 11px; line-height: 1.4; }
.batch-camera-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin-top: 10px; }
.batch-camera-fields label { min-width: 0; color: #344455; font-size: 11px; font-weight: 600; }
.batch-camera-fields input { display: block; width: 100%; min-height: 34px; margin-top: 4px; padding: 5px 7px; border: 1px solid #cbd5e1; border-radius: 6px; background: #fff; }
.batch-camera-panel > button { width: 100%; min-height: 34px; margin-top: 10px; border: 0; border-radius: 6px; background: #087b76; color: #fff; font-size: var(--font-caption); font-weight: 700; }
.batch-camera-panel > button:disabled { opacity: .55; cursor: default; }
.batch-camera-panel > p { margin: 7px 0 0; color: #0b625f; font-size: var(--font-caption); }
.batch-camera-panel > p.is-error { color: #a4262c; }
@container (max-width: 320px) {
  .major-point-card .point-heading { flex-wrap: wrap; }
  .major-point-card .point-heading > div { flex: 1 0 calc(100% - 72px); }
  .major-point-card .range-toggle { margin-left: 72px; }
}
.map-anchor-picker { display: inline-flex; align-items: center; gap: 6px; min-height: var(--map-control-height); padding: 3px 8px; border: 1px solid var(--map-control-border); border-radius: var(--map-control-radius); background: #fff; box-shadow: var(--map-control-shadow); color: #344455; font-size: var(--font-caption); white-space: nowrap; pointer-events: auto; }
.map-anchor-picker .svg-icon { width: 15px; height: 15px; color: var(--ui-accent); }
.map-anchor-picker select { max-width: 130px; min-height: 28px; border: 0; border-radius: 5px; background: #e6f1fb; color: #1769aa; font: inherit; font-weight: 600; }
.multi-route-comparison-note { margin: 12px 0 0; color: var(--ui-muted); font-size: var(--font-caption); }
.cesium-map { visibility: hidden; pointer-events: none; }
.cesium-map.visible { visibility: visible; pointer-events: auto; }
.map-top { flex-wrap: wrap; z-index: 3; }
.flight-controls { display: flex; align-items: center; gap: 6px; flex: 0 0 min(340px, 100%); min-height: var(--map-control-height); margin-left: auto; padding: 4px 7px; border: 1px solid var(--map-control-border); border-radius: var(--map-control-radius); background: #fff; box-shadow: var(--map-control-shadow); pointer-events: auto; }
.flight-toggle { display: grid; flex: none; place-items: center; width: 33px; height: 33px; padding: 0; border: 0; border-radius: 7px; background: #e6f1fb; color: #126cb7; }
.flight-toggle[aria-pressed="true"] { background: #126cb7; color: #fff; }
.flight-toggle:disabled { opacity: .45; cursor: default; }
.flight-toggle .svg-icon { width: 22px; height: 22px; }
.icon-drone { -webkit-mask-image: url('/svg/drone.svg'); mask-image: url('/svg/drone.svg'); }
.flight-speed { display: inline-flex; align-items: center; gap: 3px; flex: none; color: #526273; font-size: 11px; white-space: nowrap; }
.flight-speed select { width: 57px; min-height: 30px; padding: 0 2px; border: 1px solid #dbe2ea; border-radius: 6px; background: #f7fafc; color: #1769aa; font: inherit; font-weight: 700; }
.flight-speed select:disabled { opacity: .5; }
.flight-progress-wrap { display: flex; align-items: center; flex: 1; min-width: 60px; min-height: 30px; }
.flight-progress { width: 100%; margin: 0; accent-color: #126cb7; cursor: pointer; }
.flight-progress:disabled { opacity: .4; cursor: default; }
.flight-percent { flex: none; width: 34px; color: #526273; font-size: 11px; font-variant-numeric: tabular-nums; text-align: right; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
.map-bottom { z-index: 3; }
.map-title-group { min-width: 0; }
.base-map-switch button { white-space: nowrap; }
.view-mode-switch button { white-space: nowrap; }
.map-view-switch { position: absolute; top: 72px; left: 20px; z-index: 4; pointer-events: auto; }
@media (max-width: 720px) {
  .map-view-switch { top: 112px; left: 14px; }
}
@media (max-width: 720px) {
  .map-alert { top: 150px; }
}
.rc2-import-panel { display: grid; gap: 8px; margin: 0 0 18px; padding: 14px; border: 1px solid #c9dced; border-radius: 10px; background: #f7fbff; }
.rc2-import-heading { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 6px; }
.rc2-import-heading h3 { margin: 0; color: #173b5c; font-size: 14px; }
.rc2-import-heading button, .rc2-job-refresh { padding: 5px 8px; border: 1px solid #a9c8e4; border-radius: 6px; background: #fff; color: #1265a7; font-size: 11px; cursor: pointer; }
.rc2-import-panel p, .rc2-import-panel small, .rc2-job span { display: block; margin: 0; color: #53677c; font-size: 11px; line-height: 1.5; overflow-wrap: anywhere; }
.rc2-import-panel .form-error { color: #a4262c; }
.rc2-import-panel label { display: block; color: #344455; font-size: 12px; font-weight: 600; }
.rc2-import-panel select { display: block; width: 100%; min-height: 34px; margin-top: 5px; padding: 5px 7px; border: 1px solid #b8cddd; border-radius: 6px; background: #fff; color: #25394d; font: inherit; font-weight: 400; }
.rc2-import-action { min-height: 38px; padding: 7px 10px; border: 0; border-radius: 7px; background: #0f6cbd; color: #fff; font-size: 12px; font-weight: 700; cursor: pointer; }
.rc2-import-panel button:disabled { cursor: default; opacity: .55; }
.rc2-job { display: grid; gap: 6px; padding: 10px; border: 1px solid #bdd7ed; border-radius: 7px; background: #fff; }
.rc2-job > strong { color: #1265a7; font-size: 12px; }
.rc2-job-done { border-color: #9bd2b2; background: #f3fcf6; }
.rc2-job-done > strong { color: #167348; }
.rc2-job-failed { border-color: #edbfc1; background: #fff8f8; }
.rc2-job-failed > strong { color: #a4262c; }
.rc2-job-steps { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 4px; margin: 2px 0; padding: 0; list-style: none; }
.rc2-job-steps li { padding: 5px 3px; border-radius: 5px; background: #edf2f7; color: #607286; font-size: 10px; text-align: center; }
.rc2-job-steps li.active { background: #dceeff; color: #0f6cbd; font-weight: 700; }
.rc2-job-steps li.done { background: #ddf3e5; color: #167348; }
</style>

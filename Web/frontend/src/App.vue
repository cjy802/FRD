<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import WeatherPanels from './WeatherPanels.vue'
import RouteWeatherPanel from './RouteWeatherPanel.vue'
import EarthMap from './EarthMap.vue'
import PlaceSearch from './PlaceSearch.vue'
import PhotoAnalysisPanel from './PhotoAnalysisPanel.vue'
import VideoSurveyPanel from './VideoSurveyPanel.vue'
import CustomSelect from './CustomSelect.vue'
import InfoHint from './InfoHint.vue'
import { fetchSession, logout, type Session } from './auth'
import type { VideoCaptureTarget, VideoTelemetryPoint } from './videoSurvey'
import { osmBasemapUsesAnnotationFreeTiles, type CameraPose } from './baiduScene'
import type { RouteAssessment, WeatherLimits } from './weatherTypes'
import { weatherOnlyChecks } from './weatherPresentation'
import type { RoadPreview as Preview } from './routeTypes'
import { createFlightTimeline, sampleFlight, type FlightSample } from './flightPlayback'
import { MissionTaskError, createMissionTask, deleteAllMissionTaskFrames, deleteMissionTask, deleteMissionTaskFrame, fetchMissionTask, fetchMissionTaskFrames, saveMissionTaskFrame,
  listMissionTasks, updateMissionTask, type MissionTask, type MissionTaskSummary, type StoredMissionFrame } from './missionTasks'
import type { PhotoAnalysisJob, PhotoAnalysisResultImage, TaskVideoFrame, WaterAlertThresholds } from './photoAnalysis'
import { fetchActivePhotoAnalysisJob, fetchPhotoAnalysisJob, loadWaterAlertThresholds, saveWaterAlertThresholds } from './photoAnalysis'
import { onPipelineEvent, pipelineQueuedTaskIds, resumeTaskPipeline, STAGE_LABELS } from './analysisPipeline'
import { fetchLocationAddress, placeToPoint, type PlaceResult } from './placeSearch'

type Point = { latitude: number; longitude: number }
type MajorPoint = { id: string; latitude: number | null; longitude: number | null }
type LocationFeedback = { target: string; message: string; error?: boolean }
type NearbyPlacePicker = { target: string; center: Point; address: string; places: PlaceResult[]; accuracy: string }
const workspacePanel = ref<'settings' | 'map' | 'overview'>('overview')
const settingsPage = ref<'points' | 'mission' | 'photos'>('points')
type ModuleKey = 'route' | 'photos' | 'status' | 'dji'
const activeModule = ref<ModuleKey>('route')

function selectModule(module: ModuleKey) {
  if (module !== 'status') showRouteWeatherPoints.value = false
  const currentRouteReady = Boolean(preview.value && previewKey.value === routeKey.value)
  if ((module === 'status' || module === 'dji') && !currentRouteReady) {
    taskError.value = '请先选定航点并生成当前路线，再继续后续步骤。'
    activeModule.value = 'route'
    workspacePanel.value = 'settings'
    settingsPage.value = 'points'
    return
  }
  if (module === 'photos' && !taskRouteMatches.value) {
    taskError.value = '请先在“命名 / 导出”阶段创建任务，再导入视频和分析照片。'
    activeModule.value = currentRouteReady ? 'dji' : 'route'
    workspacePanel.value = currentRouteReady ? 'overview' : 'settings'
    settingsPage.value = 'points'
    return
  }
  activeModule.value = module
  if (module === 'photos') {
    // 模块 4 不做逐点编辑：退出 node 模式，避免整条路线渲染成编号节点。
    mapEditMode.value = 'route'
    selectedWaypointIndex.value = null
    void openActiveTaskAnalysisQueue()
  }
  if (module === 'route' || module === 'photos') {
    workspacePanel.value = 'settings'
    settingsPage.value = module === 'photos' ? 'photos' : 'points'
  } else {
    workspacePanel.value = 'overview'
  }
}
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
  speed_kmh: 10,
  clearance_m: 2,
})
type PlanningMode = 'anchors' | 'area'
type CoverageEndpoint = 'start' | 'end'
const planningMode = ref<PlanningMode>('anchors')
const coveragePolygon = ref<number[][]>([])
const defaultCoverageRoadClasses = ['motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'residential', 'unclassified', 'service']
const coverageRoadClasses = ref<string[]>([...defaultCoverageRoadClasses])
const coverageDrawEnabled = ref(false)
const coverageStartPoint = ref<Point | null>(null)
const coverageEndPoint = ref<Point | null>(null)
const coverageStartSnapPoint = ref<Point | null>(null)
const coverageEndSnapPoint = ref<Point | null>(null)
const coverageEndpointTarget = ref<CoverageEndpoint | null>(null)
const coverageRoadClassOptions = [
  { value: 'motorway', label: '高速道路' },
  { value: 'trunk', label: '快速 / 主干道' },
  { value: 'primary', label: '一级道路' },
  { value: 'secondary', label: '二级道路' },
  { value: 'tertiary', label: '三级道路' },
  { value: 'residential', label: '住宅道路' },
  { value: 'unclassified', label: '普通道路' },
  { value: 'service', label: '服务道路' },
]
const headingModeOptions = [
  { value: 'followWayline', label: '沿航线' },
  { value: 'fixed', label: '固定角度' },
  { value: 'smoothTransition', label: '平滑转向' },
  { value: 'manually', label: '手动控制' },
]
const flightSpeedOptions = [0.5, 1, 2, 4, 8].map(speed => ({ value: speed, label: `${speed}×` }))
const djiModelOptions = [
  { value: 'fly', label: 'DJI Fly · Air 3 / Air 3S / Mini 4 Pro' },
  { value: 'm30', label: 'DJI Pilot 2 · M30 / M30T' },
  { value: 'm3e', label: 'DJI Pilot 2 · Mavic 3E / 3T / 3M' },
]
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
function choosePlanningMode(mode: PlanningMode) {
  if (planningMode.value === mode) return
  if (mode === 'area') {
    roadSource.value = 'local'
    mapEditMode.value = 'route'
    if (coveragePolygon.value.length >= 3) syncAreaWeatherBounds(coveragePolygon.value)
  } else {
    const first = anchors.value[0]
    const last = anchors.value[anchors.value.length - 1]
    if (validMajorPoint(first)) Object.assign(form.start, { latitude: first.latitude, longitude: first.longitude })
    if (validMajorPoint(last)) Object.assign(form.end, { latitude: last.latitude, longitude: last.longitude })
  }
  coverageDrawEnabled.value = false
  if (mode === 'anchors') {
    coverageStartPoint.value = null
    coverageEndPoint.value = null
    coverageStartSnapPoint.value = null
    coverageEndSnapPoint.value = null
    coverageEndpointTarget.value = null
  }
  planningMode.value = mode
}
function updateCoveragePolygon(polygon: number[][]) {
  coveragePolygon.value = polygon.map(([longitude, latitude]) => [Number(longitude.toFixed(7)), Number(latitude.toFixed(7))])
  syncAreaWeatherBounds(coveragePolygon.value)
  coverageDrawEnabled.value = false
  coverageStartSnapPoint.value = null
  coverageEndSnapPoint.value = null
  coverageEndpointTarget.value = coverageStartPoint.value ? coverageEndPoint.value ? null : 'end' : 'start'
  clearBatchRange()
}
function syncAreaWeatherBounds(polygon: number[][]) {
  if (polygon.length < 3) return
  const longitudes = polygon.map(([longitude]) => longitude)
  const latitudes = polygon.map(([, latitude]) => latitude)
  Object.assign(form.start, { latitude: Math.min(...latitudes), longitude: Math.min(...longitudes) })
  Object.assign(form.end, { latitude: Math.max(...latitudes), longitude: Math.max(...longitudes) })
}
function startCoverageDraw() {
  selectModule('route')
  settingsPage.value = 'points'
  workspacePanel.value = 'map'
  mapEditMode.value = 'route'
  if (mapViewMode.value !== '2d') chooseMapViewMode('2d')
  if (!coverageStartPoint.value) coverageEndpointTarget.value = 'start'
  else if (!coverageEndPoint.value) coverageEndpointTarget.value = 'end'
  coverageDrawEnabled.value = true
}
function clearCoveragePolygon() {
  coveragePolygon.value = []
  coverageStartPoint.value = null
  coverageEndPoint.value = null
  coverageStartSnapPoint.value = null
  coverageEndSnapPoint.value = null
  coverageEndpointTarget.value = null
  coverageDrawEnabled.value = false
  preview.value = null
  previewKey.value = ''
  routeAssessment.value = null
}
function validMajorPoint(anchor: MajorPoint | undefined): anchor is MajorPoint & Point {
  return !!anchor && typeof anchor.latitude === 'number' && Number.isFinite(anchor.latitude)
    && Math.abs(anchor.latitude) <= 90 && typeof anchor.longitude === 'number'
    && Number.isFinite(anchor.longitude) && Math.abs(anchor.longitude) <= 180
}
const allAnchorsReady = computed(() => anchors.value.length >= 2 && anchors.value.every(validMajorPoint))
const coverageEndpointsReady = computed(() => !!coverageStartPoint.value && !!coverageEndPoint.value)
const routeDefinitionReady = computed(() => planningMode.value === 'area'
  ? coveragePolygon.value.length >= 3 && coverageRoadClasses.value.length > 0 && coverageEndpointsReady.value
  : allAnchorsReady.value)
const anchorChoices = computed(() => anchors.value.map((anchor, index) => ({ id: anchor.id, label: majorPointLabel(index), title: majorPointTitle(index) })))
const mapAnchors = computed(() => planningMode.value === 'area'
  ? [
      [coverageStartSnapPoint.value ?? coverageStartPoint.value, 'coverage-start', 'A', 'start'] as const,
      [coverageEndSnapPoint.value ?? coverageEndPoint.value, 'coverage-end', 'B', 'end'] as const,
    ].flatMap(([point, id, label, role]) => point ? [{ id, ...point, label, role }] : [])
  : anchors.value.flatMap((anchor, index) => validMajorPoint(anchor)
    ? [{ id: anchor.id, latitude: anchor.latitude, longitude: anchor.longitude, label: majorPointLabel(index),
      role: (index === 0 ? 'start' : index === anchors.value.length - 1 ? 'end' : 'middle') as 'start' | 'middle' | 'end' }] : []))
const selectedAnchorChoice = computed(() => anchorChoices.value.find((choice) => choice.id === selectedAnchorId.value) ?? anchorChoices.value[0])
const coverageEndpointPickerLabel = computed(() => {
  if (coverageEndpointTarget.value === 'start') return '设置区域起点 A'
  if (coverageEndpointTarget.value === 'end') return '设置区域终点 B'
  if (coverageStartPoint.value && coverageEndPoint.value) return 'A 起点 · B 终点'
  return coverageStartPoint.value ? '设置区域终点 B' : '设置区域起点 A'
})
function closeMapAnchorMenu(restoreFocus = false) {
  if (!mapAnchorMenuOpen.value) return
  mapAnchorMenuOpen.value = false
  if (restoreFocus) nextTick(() => mapAnchorPickerButton.value?.focus())
}
function toggleMapAnchorMenu() {
  if (mapAnchorPickerDisabled.value) return
  mapAnchorMenuOpen.value = !mapAnchorMenuOpen.value
  if (mapAnchorMenuOpen.value) nextTick(() => mapAnchorPickerButton.value?.focus())
}
function chooseMapAnchor(id: string) {
  if (mapAnchorPickerDisabled.value) return
  choosePlanningMode('anchors')
  selectedAnchorId.value = id
  closeMapAnchorMenu(true)
}
function chooseMapAreaMode() {
  if (mapAnchorPickerDisabled.value) return
  closeMapAnchorMenu(true)
  choosePlanningMode('area')
  startCoverageDraw()
}
function chooseMapCoverageEndpoint(endpoint: CoverageEndpoint) {
  if (mapAnchorPickerDisabled.value) return
  choosePlanningMode('area')
  coverageEndpointTarget.value = endpoint
  coverageDrawEnabled.value = false
  mapEditMode.value = 'route'
  workspacePanel.value = 'map'
  settingsPage.value = 'points'
  closeMapAnchorMenu(true)
}
function keepCoverageMode() {
  closeMapAnchorMenu(true)
}
function onMapAnchorPickerKeydown(event: KeyboardEvent) {
  if (mapAnchorPickerDisabled.value) return
  if (event.key === 'Escape') {
    event.preventDefault()
    closeMapAnchorMenu(true)
    return
  }
  if (event.key === 'ArrowDown' || event.key === 'Enter' || event.key === ' ') {
    event.preventDefault()
    if (!mapAnchorMenuOpen.value) toggleMapAnchorMenu()
  }
}
function onMapAnchorOutsidePointerDown(event: PointerEvent) {
  if (mapAnchorMenuOpen.value && !mapAnchorPicker.value?.contains(event.target as Node)) closeMapAnchorMenu()
}
watch(anchors, () => {
  const first = anchors.value[0]
  const last = anchors.value[anchors.value.length - 1]
  if (validMajorPoint(first)) Object.assign(form.start, { latitude: first.latitude, longitude: first.longitude })
  if (validMajorPoint(last)) Object.assign(form.end, { latitude: last.latitude, longitude: last.longitude })
  if (!allAnchorsReady.value) weatherJobId.value = null
}, { deep: true, flush: 'sync' })
const preview = ref<Preview | null>(null)
const settingsRoute = ref<Preview | null>(null)
const previewKey = ref('')
const weatherJobId = ref<string | null>(null)
const weatherState = ref({ status: 'idle', message: '等待天气预测。' })
const pointWeatherRiskLevel = ref<string | null>(null)
watch(allAnchorsReady, ready => { if (!ready) pointWeatherRiskLevel.value = null }, { immediate: true })
const weatherRefreshVersion = ref(0)
const routeAssessment = ref<RouteAssessment | null>(null)
const showRouteWeatherPoints = ref(false)
const routePhase = ref({ pending: false, error: '' })
const customLimitsEnabled = ref(false)
const customLimits = reactive<WeatherLimits>({ name: '自定义阈值', max_wind_m_s: 12, max_crosswind_m_s: 8,
  max_headwind_m_s: 12, max_gust_m_s: 15, max_rain_6h_mm: 5, min_visibility_m: 3000 })
const activeLimits = computed(() => customLimitsEnabled.value ? customLimits : null)
function applyWeatherLimits(limits: WeatherLimits) {
  customLimitsEnabled.value = true
  Object.assign(customLimits, limits)
}
function currentDepartureBeijing() {
  return new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 16)
}
const departureBeijing = ref(currentDepartureBeijing())
const departureAutoManaged = ref(true)
let departureClockTimer: ReturnType<typeof setTimeout> | null = null
const loading = ref(false)
const kmzLoading = ref(false)
const kmzError = ref('')
const kmzNotice = ref('')
const djiModel = ref<'fly' | 'm30' | 'm3e'>('fly')
const kmzAutoRecord = ref(true)
const activeTask = ref<MissionTask | null>(null)
const taskBusy = ref(false)
const taskError = ref('')
const taskCreateProgress = ref('')
const taskCreateNotice = ref('')
const taskFinalizeRetryId = ref('')
const createTaskDialogOpen = ref(false)
const createTaskName = ref('')
const createTaskNameInput = ref<HTMLInputElement | null>(null)
const taskFrames = ref<TaskVideoFrame[]>([])
const waterAlertThresholds = ref<WaterAlertThresholds>(loadWaterAlertThresholds())

function updateWaterAlertThresholds(value: WaterAlertThresholds) {
  waterAlertThresholds.value = value
  saveWaterAlertThresholds(value)
}
const analysisMapTargets = ref<VideoCaptureTarget[]>([])
const showAnalysisTargets = ref(true)

const session = ref<Session | null>(null)
async function signOut() {
  await logout()
  window.location.replace('/')
}
const savedTasks = ref<MissionTaskSummary[]>([])
const taskListOpen = ref(false)
const taskListLoading = ref(false)
const taskListError = ref('')
const pendingDeleteTaskId = ref('')
const pendingDeleteAll = ref(false)
const deletingTaskId = ref('')
const ACTIVE_TASK_STORAGE_KEY = 'low-altitude-current-mission-task'
type Rc2Status = {
  available: boolean
  platform: string
  message: string
  generation_directory: string
  script_available: boolean
  busy: boolean
}
type Rc2Target = { name: string; size?: number; type?: string }
type Rc2FolderTarget = { folder: string; kmz_name: string; subpath: string; name_matches_folder?: boolean }
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
const rc2Folders = ref<Rc2FolderTarget[]>([])
const rc2TargetOptions = computed(() => [
  { value: '', label: rc2Folders.value.length ? '请选择要替换的占位航线文件夹' : '请先刷新检测遥控器' },
  ...rc2Folders.value.map(entry => ({
    value: entry.folder,
    label: entry.name_matches_folder ? entry.folder : `${entry.folder}（kmz 名不一致：${entry.kmz_name}）`,
  })),
])
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
let rc2ConnectionPromise: Promise<void> | null = null
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
const waypointEditorTrigger = ref<HTMLButtonElement | null>(null)
const waypointEditorDialog = ref<HTMLElement | null>(null)
const batchBoundaryIds = ref<string[]>([])
const batchHeadingInput = ref('')
const batchPitchInput = ref('')
const batchFeedback = ref<{ message: string; error: boolean } | null>(null)
const waypointEditorFeedback = ref('')
const batchPanelElement = ref<HTMLElement | null>(null)
const checkingPoint = ref(false)
const locatingPoint = ref<string | null>(null)
const locationFeedback = ref<LocationFeedback | null>(null)
const nearbyPlacePicker = ref<NearbyPlacePicker | null>(null)
let nearbyPlacesController: AbortController | null = null
let locationRequestVersion = 0
const error = ref('')
const placeSelectionFeedback = ref<{ message: string; error: boolean } | null>(null)
const baseMap = ref<'current' | 'satellite'>('current')
// 卫星底图走天地图影像(img),没有 TK 就不提供该选项。
const tiandituSatelliteAvailable = osmBasemapUsesAnnotationFreeTiles()
const earthMapRef = ref<InstanceType<typeof EarthMap> | null>(null)
const pointRouteViewMode = ref<'2d' | '3d'>('3d')
const areaRouteViewMode = ref<'2d' | '3d'>('2d')
const mapViewMode = computed<'2d' | '3d'>(() => planningMode.value === 'area' ? areaRouteViewMode.value : pointRouteViewMode.value)
const mapEditMode = ref<'route' | 'node'>('route')
const videoTelemetryTrack = ref<number[][]>([])
const videoTelemetryPosition = ref<VideoTelemetryPoint | null>(null)
const videoCaptureTargets = ref<VideoCaptureTarget[]>([])
function setVideoTelemetryTrack(track: number[][]) { videoTelemetryTrack.value = track }
function setVideoTelemetryPosition(position: VideoTelemetryPoint | null) { videoTelemetryPosition.value = position }
function setVideoCaptureTargets(targets: VideoCaptureTarget[]) { videoCaptureTargets.value = targets }
// 地图只呈现有坐标的终审确认点（无 GPS 帧不上图，仍可下发到现场端）。
// 旧版页面写入的标记没有 analysis_kind 字段，一律不再上图。
const mapAnalysisTargets = computed(() => analysisMapTargets.value.filter(target =>
  target.analysis_kind === 'confirmed' && Number.isFinite(target.latitude) && Number.isFinite(target.longitude)))
const mapCaptureTargets = computed(() => showAnalysisTargets.value
  ? [...videoCaptureTargets.value, ...mapAnalysisTargets.value]
  : [...videoCaptureTargets.value])

async function openCreateTaskDialog() {
  if (!canFinalizeTask.value) {
    taskError.value = '请等待当前路线计算完成后再创建任务。'
    return
  }
  taskError.value = ''
  taskCreateNotice.value = ''
  createTaskName.value = taskFinalizeRetryId.value ? activeTask.value?.name ?? '' : ''
  createTaskDialogOpen.value = true
  await nextTick()
  createTaskNameInput.value?.focus()
}

function closeCreateTaskDialog() {
  if (taskBusy.value) return
  createTaskDialogOpen.value = false
}

async function createCurrentTask() {
  const name = createTaskName.value.trim()
  if (!name) {
    taskError.value = '请填写任务名称。'
    createTaskNameInput.value?.focus()
    return
  }
  if (!canFinalizeTask.value) {
    taskError.value = '路线已变化或仍在计算，请返回选定航线并等待计算完成。'
    return
  }
  taskBusy.value = true
  taskError.value = ''
  try {
    let taskId = taskFinalizeRetryId.value
    if (!taskId) {
      const created = await createMissionTask(taskRouteSnapshot.value, departureBeijing.value, departureAutoManaged.value, {
        name,
        weather: routeAssessment.value as unknown as Record<string, unknown> | null,
        aircraft: { drone_model: djiModel.value, waypoint_settings: waypointSettings.value },
      })
      activeTask.value = created
      taskId = created.task_id
      taskFinalizeRetryId.value = taskId
      localStorage.setItem(ACTIVE_TASK_STORAGE_KEY, taskId)
      taskFrames.value = []
      analysisMapTargets.value = []
      pendingReviewTargets.value = []
      selectedWaypointIndex.value = null
      void refreshMissionTaskList()
    }

    taskCreateProgress.value = '正在生成并归档 DJI KMZ…'
    const kmz = await generateTaskKmz(taskId)
    let importedToRc2 = false
    let fallbackReason = ''
    if (djiModel.value !== 'fly') {
      fallbackReason = '当前机型不能通过 DJI RC 2 自动导入。'
    } else {
      taskCreateProgress.value = '正在检测 DJI RC 2…'
      await refreshRc2Connection()
      if (!rc2Status.value?.available || rc2Status.value.busy) {
        const detectionError = rc2Error.value || rc2Status.value?.message || '未检测到可用的 DJI RC 2。'
        if (isRc2DeviceUnavailable(detectionError)) {
          markRc2DeviceUnavailable()
          fallbackReason = '未检测到已解锁的 DJI RC 2。'
        } else fallbackReason = detectionError
      } else {
        const selectedTarget = rc2Folders.value.find((entry) => entry.folder === rc2TargetName.value)
        const target = selectedTarget ?? (rc2Folders.value.length === 1 ? rc2Folders.value[0] : null)
        if (!target) {
          fallbackReason = rc2Folders.value.length
            ? '检测到多条占位航线文件夹但未选择目标。'
            : '检测到 DJI RC 2，但没有可替换的占位航线文件夹。'
        } else {
          try {
            taskCreateProgress.value = `正在导入 DJI RC 2：${target.folder}…`
            const queued = await startRc2Import(target.folder)
            const result = await waitForRc2Job(queued.job_id)
            if (result.status === 'succeeded') importedToRc2 = true
            else if (isRc2DeviceUnavailable(result.message)) {
              markRc2DeviceUnavailable()
              fallbackReason = '未检测到已解锁的 DJI RC 2。'
            } else fallbackReason = result.message || 'DJI RC 2 导入未完成。'
          } catch (cause) {
            const message = cause instanceof Error ? cause.message : 'DJI RC 2 导入失败。'
            if (isRc2DeviceUnavailable(message)) {
              markRc2DeviceUnavailable()
              fallbackReason = '未检测到已解锁的 DJI RC 2。'
            } else {
              fallbackReason = message
              rc2Error.value = fallbackReason
            }
          }
        }
      }
    }

    if (importedToRc2) {
      taskCreateNotice.value = `任务已创建；KMZ 已归档并导入到 DJI RC 2（${rc2Job.value?.target_file_name ?? ''}）。`
    } else {
      rc2Error.value = ''
      if (rc2Job.value?.status === 'failed') rc2Job.value = null
      taskCreateProgress.value = '无法完成设备导入，正在下载 KMZ…'
      downloadKmz(kmz)
      taskCreateNotice.value = `任务已创建；KMZ 已归档并下载。${fallbackReason}`
    }
    activeTask.value = await fetchMissionTask(taskId).catch(() => activeTask.value)
    taskFinalizeRetryId.value = ''
    createTaskDialogOpen.value = false
    void refreshMissionTaskList()
  } catch (cause) {
    const detail = cause instanceof Error ? cause.message : '创建任务或生成 KMZ 失败。'
    taskError.value = taskFinalizeRetryId.value ? `任务已保存，但 KMZ 流程未完成：${detail}。可再次确认以重试。` : detail
  } finally {
    taskBusy.value = false
    taskCreateProgress.value = ''
  }
}

async function generateTaskKmz(taskId: string): Promise<Blob> {
  kmzLoading.value = true
  kmzError.value = ''
  kmzNotice.value = ''
  try {
    const response = await fetch(`/api/routes/export-kmz?task_id=${encodeURIComponent(taskId)}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(djiKmzRequestBody()),
    })
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}))
      throw new Error(typeof payload.detail === 'string' ? payload.detail : 'DJI KMZ 生成失败。')
    }
    const mergeNotices = (response.headers.get('X-Kmz-Merges') ?? '').split(',').filter(Boolean).map((pair) => {
      const [dropped, kept] = pair.split(':').map(Number)
      if (!Number.isInteger(dropped) || !Number.isInteger(kept)) return ''
      return `航点 ${dropped + 1} 与 ${kept + 1} 位置过近，已合并为一个（保留航点 ${kept + 1} 的设置）`
    }).filter(Boolean)
    kmzNotice.value = mergeNotices.join('；')
    return await response.blob()
  } catch (cause) {
    kmzError.value = cause instanceof Error ? cause.message : 'DJI KMZ 生成失败。'
    throw cause
  } finally {
    kmzLoading.value = false
  }
}

function downloadKmz(blob: Blob) {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = 'dji_waypoint_draft.kmz'
  document.body.appendChild(link)
  link.click()
  link.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}

async function persistTask(changes: Parameters<typeof updateMissionTask>[1]) {
  if (!activeTask.value || !taskRouteMatches.value) return
  try {
    activeTask.value = await updateMissionTask(activeTask.value.task_id, changes)
    if (taskListOpen.value) void refreshMissionTaskList()
  } catch (cause) {
    taskError.value = cause instanceof Error ? cause.message : '保存任务状态失败。'
  }
}

async function recordTaskWeather(assessment: RouteAssessment | null) {
  routeAssessment.value = assessment
  if (!assessment) return
  await persistTask({ weather: assessment as unknown as Record<string, unknown> })
}

const activeAnalysisJobId = ref('')
const analysisQueueOpen = ref(false)

async function openActiveTaskAnalysisQueue() {
  const taskId = activeTask.value?.task_id
  const jobId = activeAnalysisJobId.value
  if (!taskId || !jobId) {
    analysisQueueOpen.value = false
    return
  }
  try {
    const job = await fetchPhotoAnalysisJob(jobId)
    if (activeTask.value?.task_id !== taskId || activeAnalysisJobId.value !== jobId) return
    analysisQueueOpen.value = job.state === 'pending' || job.state === 'running'
  } catch {
    if (activeTask.value?.task_id === taskId && activeAnalysisJobId.value === jobId) analysisQueueOpen.value = false
  }
}

type AnalysisFrameMeta = { time_seconds?: number | null; latitude?: number | null; longitude?: number | null; altitude_m?: number | null; gimbal_pitch_deg?: number | null }

function analysisTargetsFromJob(job: PhotoAnalysisJob): VideoCaptureTarget[] {
  const images = job.results?.images ?? []
  // 无 GPS 视频的帧没有坐标，也要进人工复核——现场端对无坐标点显示“坐标待补充”。
  if (job.mode !== 'frames') {
    // legacy 任务：帧元数据按提交顺序放在 context.frames 里。
    const frames = job.context.frames ?? []
    const pairs = images.map((image, index) => ({
      image, meta: frames[index] as AnalysisFrameMeta | undefined,
    }))
    return buildTargets(pairs.filter((pair): pair is { image: PhotoAnalysisResultImage; meta: AnalysisFrameMeta } => Boolean(pair.meta)), job)
  }
  // 队列任务：每个 item 自带帧元数据，按 name 对应识别结果。
  const metaByName = new Map((job.items ?? []).map(item => [item.name, item]))
  const pairs = images.map(image => ({ image, meta: metaByName.get(image.name)?.meta }))
  return buildTargets(pairs.filter((pair): pair is { image: PhotoAnalysisResultImage; meta: AnalysisFrameMeta } => Boolean(pair.meta)), job)
}

function buildTargets(pairs: { image: PhotoAnalysisResultImage; meta: AnalysisFrameMeta }[], job: PhotoAnalysisJob): VideoCaptureTarget[] {
  // 复检 job 的输入帧都是 YOLO 检出帧：GLM 再检出即“双模型确认”。
  const isRecheck = Boolean(job.context?.recheck_of_job_id)
  // 只有终审确认的点才进地图和现场端；首级初筛结果只留在照片分析面板里，
  // 不再生成候选标记，避免“复检未检出”的帧堆在地图上。
  if (!isRecheck) return []
  const confirmWord = '双模型确认'
  // 不设数量上限：全部确认帧交给管理员逐张复核勾选。
  return pairs
    .filter(({ image }) => (image.suspected_pixels ?? 0) > 0)
    .sort((first, second) => (second.image.estimated_area_m2 ?? 0) - (first.image.estimated_area_m2 ?? 0))
    .map(({ image, meta }, index): VideoCaptureTarget => {
      const hasCoords = Number.isFinite(meta.latitude) && Number.isFinite(meta.longitude)
      const positionText = hasCoords ? describeRoutePosition(meta.latitude as number, meta.longitude as number) : ''
      const areaText = `占比 ${(image.suspected_fraction * 100).toFixed(1)}%`
      return {
        id: `analysis-${job.job_id}-${image.name}`,
        label: `疑似积水（${confirmWord}）${index + 1}${positionText ? ` · ${positionText}` : ''} · ${hasCoords ? `${(meta.latitude as number).toFixed(5)}, ${(meta.longitude as number).toFixed(5)}` : '坐标待补充'} · ${areaText}`,
        created_at: new Date().toISOString(),
        analysis_kind: 'confirmed',
        badge: '✓',
        time_seconds: meta.time_seconds ?? 0,
        latitude: hasCoords ? meta.latitude as number : null,
        longitude: hasCoords ? meta.longitude as number : null,
        altitude_m: meta.altitude_m ?? null,
        heading_deg: null,
        gimbal_pitch_deg: meta.gimbal_pitch_deg ?? null,
      }
    })
}

type TaskRouteLike = {
  geometry?: { coordinates?: number[][] } | null
  segments?: Array<{ start_index?: number; end_index?: number; road_names?: string[] }> | null
}

// 把帧坐标投影到任务路线折线：返回“所在路段路名 · 距路线起点约 X m”。
// 旧任务快照没有 segments 时只报距起点；没有路线则返回空串。
function describeRoutePosition(latitude: number, longitude: number): string {
  const route = activeTask.value?.route as TaskRouteLike | undefined
  const coordinates = route?.geometry?.coordinates
  if (!Array.isArray(coordinates) || coordinates.length < 2) return ''
  const metresPerDegree = 111320
  const lonScale = Math.cos((latitude * Math.PI) / 180)
  const pointX = longitude * lonScale * metresPerDegree
  const pointY = latitude * metresPerDegree
  const vertices = coordinates.map(item => [item[0] * lonScale * metresPerDegree, item[1] * metresPerDegree])
  let travelledM = 0
  let best: { alongM: number; distanceM: number; edgeIndex: number } | null = null
  for (let index = 0; index < vertices.length - 1; index += 1) {
    const [x1, y1] = vertices[index]
    const [x2, y2] = vertices[index + 1]
    const dx = x2 - x1
    const dy = y2 - y1
    const lengthM = Math.hypot(dx, dy)
    let t = lengthM > 0 ? ((pointX - x1) * dx + (pointY - y1) * dy) / (lengthM * lengthM) : 0
    t = Math.max(0, Math.min(1, t))
    const distanceM = Math.hypot(pointX - (x1 + t * dx), pointY - (y1 + t * dy))
    if (best === null || distanceM < best.distanceM) best = { alongM: travelledM + t * lengthM, distanceM, edgeIndex: index }
    travelledM += lengthM
  }
  if (best === null) return ''
  const parts: string[] = []
  const segments = route?.segments
  if (Array.isArray(segments)) {
    const segment = segments.find(item =>
      typeof item.start_index === 'number' && typeof item.end_index === 'number'
      && item.start_index <= best.edgeIndex && best.edgeIndex < item.end_index)
    const roadName = segment?.road_names?.[0]
    if (roadName) parts.push(roadName)
  }
  const alongM = Math.round(best.alongM)
  parts.push(alongM >= 1000 ? `距路线起点约 ${(alongM / 1000).toFixed(1)} km` : `距路线起点约 ${alongM} m`)
  return parts.join(' · ')
}

// 复检完成的结果先进待复核暂存区，等管理员在照片面板逐张勾选后显式下发；
// 只有下发动作才会写进任务的 analysis_targets（现场端只认这份快照）。
const pendingReviewTargets = ref<VideoCaptureTarget[]>([])

async function absorbAnalysisResults(job: PhotoAnalysisJob) {
  if (!activeTask.value) return
  if (!job.context?.recheck_of_job_id) return
  pendingReviewTargets.value = analysisTargetsFromJob(job)
}

async function dispatchAnalysisReview(ids: string[]) {
  if (!activeTask.value || !ids.length) return
  // 下发 = 用本次人工勾选结果整体替换已下发点，以最新复核为准。
  const chosen = pendingReviewTargets.value.filter(target => ids.includes(target.id))
  if (!chosen.length) return
  analysisMapTargets.value = chosen
  pendingReviewTargets.value = []
  await persistTask({ analysis_targets: chosen as unknown as Record<string, unknown>[] })
}

async function clearAnalysisTargets() {
  if (!activeTask.value || !analysisMapTargets.value.length) return
  analysisMapTargets.value = []
      pendingReviewTargets.value = []
  await persistTask({ analysis_targets: [] as unknown as Record<string, unknown>[] })
}

// GLM 终审未检出、管理员看图后人工确认有积水的帧：加进待复核下发列表
// （再点一次同一张可移除）。标签带“人工确认”字样与模型确认区分。
function toggleManualConfirm(payload: { jobId: string; image: PhotoAnalysisResultImage; meta: AnalysisFrameMeta }) {
  if (!activeTask.value) return
  const id = `analysis-${payload.jobId}-${payload.image.name}`
  if (pendingReviewTargets.value.some(target => target.id === id)) {
    pendingReviewTargets.value = pendingReviewTargets.value.filter(target => target.id !== id)
    return
  }
  const meta = payload.meta
  const hasCoords = Number.isFinite(meta.latitude) && Number.isFinite(meta.longitude)
  const positionText = hasCoords ? describeRoutePosition(meta.latitude as number, meta.longitude as number) : ''
  const coordsText = hasCoords ? `${(meta.latitude as number).toFixed(5)}, ${(meta.longitude as number).toFixed(5)}` : '坐标待补充'
  pendingReviewTargets.value = [...pendingReviewTargets.value, {
    id,
    label: `疑似积水（人工确认）${pendingReviewTargets.value.length + 1}${positionText ? ` · ${positionText}` : ''} · ${coordsText} · GLM 未检出，人工看图确认`,
    created_at: new Date().toISOString(),
    analysis_kind: 'confirmed' as const,
    badge: '✓',
    time_seconds: meta.time_seconds ?? 0,
    latitude: hasCoords ? meta.latitude as number : null,
    longitude: hasCoords ? meta.longitude as number : null,
    altitude_m: meta.altitude_m ?? null,
    heading_deg: null,
    gimbal_pitch_deg: meta.gimbal_pitch_deg ?? null,
  }]
}

// —— 识别流水线全局编排接线：链在 analysisPipeline 单例里推进，页面级生命周期 ——
const offPipelineEvents = onPipelineEvent(event => {
  if (event.type === 'job-submitted') {
    activeAnalysisJobId.value = event.jobId
  } else if (event.type === 'job-update') {
    // 只吸收当前任务的 job：切走后落盘会把确认点写进别的任务；
    // 切回时面板 attachJob 会 emit 终态补一次吸收。
    if (activeTask.value?.task_id === event.taskId) void absorbAnalysisResults(event.job)
  } else {
    // 链结束/中断：立即刷一次徽章，不等下一个轮询周期。
    void pollAnalysisBadge()
  }
})

// —— 任务列表识别进度徽章：后端单槽，全局只有一个活跃 job，5s 轮询足够 ——
const analysisBadge = ref<{ taskId: string; text: string } | null>(null)
let analysisBadgeTimer: number | null = null
async function pollAnalysisBadge(): Promise<void> {
  try {
    const { active } = await fetchActivePhotoAnalysisJob()
    if (active?.task_id) {
      const stage = STAGE_LABELS[active.model_id ?? ''] ?? '识别中'
      const counts = active.counts
      analysisBadge.value = { taskId: active.task_id, text: counts ? `${stage} ${counts.done}/${counts.total}` : stage }
    } else {
      analysisBadge.value = null
    }
  } catch {
    analysisBadge.value = null
  }
}
function scheduleAnalysisBadgePoll(): void {
  if (analysisBadgeTimer !== null) window.clearTimeout(analysisBadgeTimer)
  analysisBadgeTimer = window.setTimeout(() => { void pollAnalysisBadge().finally(scheduleAnalysisBadgePoll) }, 5000)
}
onMounted(() => { void pollAnalysisBadge().finally(scheduleAnalysisBadgePoll) })
onUnmounted(() => {
  offPipelineEvents()
  if (analysisBadgeTimer !== null) window.clearTimeout(analysisBadgeTimer)
})

function onAnalysisJobStarted(jobId: string) {
  activeAnalysisJobId.value = jobId
}

async function markTaskFlightStarted() {
  if (!activeTask.value || !taskRouteMatches.value || !activeTask.value.export_file_name) return
  departureAutoManaged.value = false
  await persistTask({ status: 'in_flight', flight_started_at: new Date().toISOString(),
    departure_time: departureBeijing.value, departure_auto_managed: false })
}

async function addTaskFrame(frame: TaskVideoFrame) {
  const file = frame.file
  if (!activeTask.value || !taskRouteMatches.value || !file) return
  taskBusy.value = true
  taskError.value = ''
  try {
    const saved = await saveMissionTaskFrame(activeTask.value.task_id, { ...frame, file })
    taskFrames.value = [...taskFrames.value, { ...saved, file }]
  } catch (cause) {
    taskError.value = cause instanceof Error ? cause.message : '保存任务关键帧失败。'
  } finally {
    taskBusy.value = false
  }
}

async function addManualTaskFrames(frames: TaskVideoFrame[]) {
  for (const frame of frames) {
    if (taskFrames.value.length >= 300) {
      taskError.value = '每个任务最多保留 300 张关键帧；余下照片没有加入。'
      break
    }
    await addTaskFrame(frame)
  }
}

async function removeTaskFrame(index: number) {
  const frame = taskFrames.value[index]
  if (!frame) return
  try {
    if (activeTask.value && frame.frame_id) await deleteMissionTaskFrame(activeTask.value.task_id, frame.frame_id)
    taskFrames.value = taskFrames.value.filter((_, frameIndex) => frameIndex !== index)
  } catch (cause) {
    // 帧只增不删；404 仅在列表里是旧记录（如任务已重建）时出现，直接本地移除。
    if (cause instanceof MissionTaskError && cause.status === 404) {
      taskFrames.value = taskFrames.value.filter((_, frameIndex) => frameIndex !== index)
      return
    }
    taskError.value = cause instanceof Error ? cause.message : '移除任务关键帧失败。'
  }
}

async function removeAllTaskFrames() {
  if (!activeTask.value || !taskFrames.value.length) return
  taskBusy.value = true
  taskError.value = ''
  try {
    await deleteAllMissionTaskFrames(activeTask.value.task_id)
    taskFrames.value = []
  } catch (cause) {
    if (cause instanceof MissionTaskError && cause.status === 404) {
      taskFrames.value = []
      return
    }
    taskError.value = cause instanceof Error ? cause.message : '清空任务关键帧失败。'
  } finally {
    taskBusy.value = false
  }
}

async function refreshTaskFrames(taskId: string) {
  // 只拉取帧记录；图片文件由 <img> 直接引用服务器 URL（长缓存），
  // 不再把整批 JPEG 下载进内存构造 File。
  const records: StoredMissionFrame[] = await fetchMissionTaskFrames(taskId)
  if (activeTask.value?.task_id === taskId) taskFrames.value = records.map(record => ({ ...record, file: null }))
}

function onTaskFrameSaved(frame: TaskVideoFrame) {
  if (taskFrames.value.length >= 300) return
  taskFrames.value = [...taskFrames.value, frame]
}

async function restoreCurrentTask() {
  const taskId = localStorage.getItem(ACTIVE_TASK_STORAGE_KEY)
  if (!taskId) return
  try {
    applyMissionTask(await fetchMissionTask(taskId))
    await refreshTaskFrames(taskId)
  } catch {
    localStorage.removeItem(ACTIVE_TASK_STORAGE_KEY)
    activeTask.value = null
  }
}

function applyMissionTask(task: MissionTask) {
  activeTask.value = task
  localStorage.setItem(ACTIVE_TASK_STORAGE_KEY, task.task_id)
  // 任务边界即状态边界：视频轨迹/近拍点/节点编辑态随任务切换一并重置，
  // 不依赖子组件 prop 联动（prop 值不变时会漏清）。
  videoTelemetryTrack.value = []
  videoTelemetryPosition.value = null
  videoCaptureTargets.value = []
  mapEditMode.value = 'route'
  selectedWaypointIndex.value = null
  preview.value = null
  previewKey.value = ''
  settingsRoute.value = null
  waypointSettings.value = []
  const savedCoverage = task.route.coverage_area as { polygon?: unknown; road_classes?: unknown; start?: unknown; end?: unknown; point_anchors?: unknown } | null | undefined
  if (savedCoverage && Array.isArray(savedCoverage.polygon) && savedCoverage.polygon.length >= 3) {
    planningMode.value = 'area'
    coveragePolygon.value = (savedCoverage.polygon as number[][]).map(([longitude, latitude]) => [Number(longitude), Number(latitude)])
    syncAreaWeatherBounds(coveragePolygon.value)
    coverageRoadClasses.value = Array.isArray(savedCoverage.road_classes)
      ? savedCoverage.road_classes.filter((value): value is string => typeof value === 'string')
      : [...defaultCoverageRoadClasses]
    const routeStart = savedCoverage.start && typeof savedCoverage.start === 'object'
      ? savedCoverage.start as Record<string, unknown>
      : Array.isArray(task.route.anchors) ? (task.route.anchors as Array<Record<string, unknown>>)[0] : null
    const routeEnd = savedCoverage.end && typeof savedCoverage.end === 'object'
      ? savedCoverage.end as Record<string, unknown>
      : Array.isArray(task.route.anchors) ? (task.route.anchors as Array<Record<string, unknown>>)[task.route.anchors.length - 1] ?? null : null
    coverageStartPoint.value = routeStart && typeof routeStart.latitude === 'number' && typeof routeStart.longitude === 'number'
      ? { latitude: routeStart.latitude, longitude: routeStart.longitude } : null
    coverageEndPoint.value = routeEnd && typeof routeEnd.latitude === 'number' && typeof routeEnd.longitude === 'number'
      ? { latitude: routeEnd.latitude, longitude: routeEnd.longitude } : null
    coverageStartSnapPoint.value = null
    coverageEndSnapPoint.value = null
    coverageEndpointTarget.value = coverageStartPoint.value && coverageEndPoint.value ? null : coverageStartPoint.value ? 'end' : 'start'
  } else {
    planningMode.value = 'anchors'
    coveragePolygon.value = []
    coverageRoadClasses.value = [...defaultCoverageRoadClasses]
    coverageStartPoint.value = null
    coverageEndPoint.value = null
    coverageStartSnapPoint.value = null
    coverageEndSnapPoint.value = null
    coverageEndpointTarget.value = null
  }
  taskFrames.value = []
  analysisMapTargets.value = (task.analysis_targets ?? []) as unknown as VideoCaptureTarget[]
  pendingReviewTargets.value = []
  activeAnalysisJobId.value = task.analysis_job_ids?.length ? task.analysis_job_ids[task.analysis_job_ids.length - 1] : ''
  analysisQueueOpen.value = false
  // 切回任务自动续跑：流水线若在页面关闭/刷新期间断链（初筛 done 但 GLM 未提交）
  // 则补交终审；仍在跑则由全局单例接管轮询；单槽被其他任务占用时自动排队。
  if (task.task_id && (task.analysis_job_ids?.length ?? 0) > 0) {
    void resumeTaskPipeline(task.task_id, task.analysis_job_ids ?? [])
  }
  const route = task.route
  const savedAnchors = planningMode.value === 'area' && Array.isArray(savedCoverage?.point_anchors)
    ? savedCoverage.point_anchors : route.anchors
  if (Array.isArray(savedAnchors) && savedAnchors.length >= 2) {
    anchors.value = savedAnchors.map((anchor: any) => ({ id: String(anchor.id), latitude: Number(anchor.latitude), longitude: Number(anchor.longitude) }))
    selectedAnchorId.value = anchors.value[0].id
  } else {
    // 任务没有可用的主点记录：重置为默认主点，而不是沿用上一个任务的 A/B。
    anchors.value = [
      { id: 'anchor-1', latitude: form.start.latitude, longitude: form.start.longitude },
      { id: 'anchor-2', latitude: form.end.latitude, longitude: form.end.longitude },
    ]
    nextAnchorNumber = 3
    selectedAnchorId.value = 'anchor-1'
  }
  if (planningMode.value === 'area') syncAreaWeatherBounds(coveragePolygon.value)
  if (route.road_source === 'local' || route.road_source === 'online') roadSource.value = route.road_source
  if (typeof route.altitude_m === 'number') form.altitude_m = route.altitude_m
  if (typeof route.speed_kmh === 'number') form.speed_kmh = route.speed_kmh
  if (typeof route.clearance_m === 'number') form.clearance_m = route.clearance_m
  if (task.aircraft?.drone_model === 'fly' || task.aircraft?.drone_model === 'm30' || task.aircraft?.drone_model === 'm3e') {
    djiModel.value = task.aircraft.drone_model
  }
  departureAutoManaged.value = task.status !== 'in_flight' && task.status !== 'completed'
  departureBeijing.value = departureAutoManaged.value ? currentDepartureBeijing() : task.departure_time
  routeAssessment.value = task.weather as unknown as RouteAssessment | null
}

async function refreshMissionTaskList() {
  taskListLoading.value = true
  taskListError.value = ''
  pendingDeleteAll.value = false
  try {
    savedTasks.value = await listMissionTasks()
  } catch (cause) {
    taskListError.value = cause instanceof Error ? cause.message : '读取已保存任务失败。'
  } finally {
    taskListLoading.value = false
  }
}

function toggleTaskList() {
  taskListOpen.value = !taskListOpen.value
  pendingDeleteAll.value = false
  if (taskListOpen.value) void refreshMissionTaskList()
}

function closeTaskList() {
  taskListOpen.value = false
  pendingDeleteAll.value = false
}

function requestDeleteMissionTask(taskId: string) {
  pendingDeleteTaskId.value = pendingDeleteTaskId.value === taskId ? '' : taskId
  pendingDeleteAll.value = false
  taskListError.value = ''
}

function openSavedTask(taskId: string) {
  if (taskBusy.value || deletingTaskId.value) return
  void selectSavedTask(taskId)
}

function requestDeleteAllMissionTasks() {
  if (!savedTasks.value.length) return
  if (!pendingDeleteAll.value) {
    pendingDeleteAll.value = true
    pendingDeleteTaskId.value = ''
    taskListError.value = ''
    return
  }
  void deleteAllMissionTasks()
}

async function deleteAllMissionTasks() {
  if (deletingTaskId.value || !savedTasks.value.length) return
  deletingTaskId.value = '__all__'
  taskListError.value = ''
  const targets = savedTasks.value.slice()
  const activeId = activeTask.value?.task_id
  try {
    await Promise.all(targets.map((task) => deleteMissionTask(task.task_id)))
    savedTasks.value = []
    pendingDeleteTaskId.value = ''
    if (taskFinalizeRetryId.value) {
      // 全部任务已删除，任何“KMZ 未完成待重试”状态都已失效。
      taskFinalizeRetryId.value = ''
      taskError.value = ''
    }
    if (activeId && targets.some((task) => task.task_id === activeId)) {
      localStorage.removeItem(ACTIVE_TASK_STORAGE_KEY)
      activeTask.value = null
      taskFrames.value = []
      analysisMapTargets.value = []
      pendingReviewTargets.value = []
      selectedWaypointIndex.value = null
      routeAssessment.value = null
      selectModule('route')
    }
  } catch (cause) {
    taskListError.value = cause instanceof Error ? cause.message : '删除全部任务失败。'
    await refreshMissionTaskList()
  } finally {
    deletingTaskId.value = ''
    pendingDeleteAll.value = false
  }
}

async function deleteSavedMissionTask(task: MissionTaskSummary) {
  if (pendingDeleteTaskId.value !== task.task_id || deletingTaskId.value) return
  deletingTaskId.value = task.task_id
  taskListError.value = ''
  try {
    await deleteMissionTask(task.task_id)
    savedTasks.value = savedTasks.value.filter((item) => item.task_id !== task.task_id)
    pendingDeleteTaskId.value = ''
    if (taskFinalizeRetryId.value === task.task_id) {
      // 待重试的任务已被删除；保留重试状态会让“再次确认”拿着旧编号 404。
      taskFinalizeRetryId.value = ''
      taskError.value = ''
    }
    if (activeTask.value?.task_id === task.task_id) {
      localStorage.removeItem(ACTIVE_TASK_STORAGE_KEY)
      activeTask.value = null
      taskFrames.value = []
      analysisMapTargets.value = []
      pendingReviewTargets.value = []
      selectedWaypointIndex.value = null
      routeAssessment.value = null
      selectModule('route')
    }
  } catch (cause) {
    taskListError.value = cause instanceof Error ? cause.message : '删除任务失败。'
  } finally {
    deletingTaskId.value = ''
  }
}

function taskStatusText(status: MissionTaskSummary['status']) {
  return ({ planning: '规划中', ready: '已准备', in_flight: '已开始', completed: '已完成' } as Record<string, string>)[status] ?? status
}

function taskDateText(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  })
}

async function selectSavedTask(taskId: string) {
  if (taskBusy.value || activeTask.value?.task_id === taskId) { closeTaskList(); return }
  taskBusy.value = true
  taskListError.value = ''
  cancelCalculation()
  stopFlight()
  try {
    applyMissionTask(await fetchMissionTask(taskId))
    selectModule('route')
    await nextTick()
    await refreshTaskFrames(taskId)
    await calculate(false)
    restoreTaskWaypointSettings()
    closeTaskList()
  } catch (cause) {
    taskListError.value = cause instanceof Error ? cause.message : '载入已保存任务失败。'
  } finally {
    taskBusy.value = false
  }
}

function restoreTaskWaypointSettings() {
  const saved = activeTask.value?.aircraft?.waypoint_settings
  if (!Array.isArray(saved)) return
  if (saved.length !== waypointSettings.value.length) {
    // 路线重算后的点数与保存时不一致（道路数据变化等），逐点设置无法对位。
    taskError.value = '任务的逐点高度/视角设置与当前重算路线的点数不一致，未恢复。'
    return
  }
  waypointSettings.value = waypointSettings.value.map((setting, index) => ({ ...setting, ...saved[index] }))
  scheduleWaypointRender()
}

async function onTaskSurveyChange() {
  if (!activeTask.value || !taskRouteMatches.value) return
  const updated = await fetchMissionTask(activeTask.value.task_id).catch(() => null)
  if (updated) activeTask.value = updated
}

watch(djiModel, (model) => {
  void persistTask({ aircraft: { drone_model: model, waypoint_settings: waypointSettings.value } })
})
let taskDepartureSaveTimer: ReturnType<typeof setTimeout> | null = null
watch([departureBeijing, departureAutoManaged], () => {
  if (!activeTask.value || !taskRouteMatches.value) return
  if (taskDepartureSaveTimer) clearTimeout(taskDepartureSaveTimer)
  taskDepartureSaveTimer = setTimeout(() => {
    void persistTask({ departure_time: departureBeijing.value, departure_auto_managed: departureAutoManaged.value })
  }, 250)
})
let taskSettingsSaveTimer: ReturnType<typeof setTimeout> | null = null
watch(waypointSettings, () => {
  if (activeModule.value !== 'dji') return
  if (taskSettingsSaveTimer) clearTimeout(taskSettingsSaveTimer)
  taskSettingsSaveTimer = setTimeout(() => {
    void persistTask({ aircraft: { drone_model: djiModel.value, waypoint_settings: waypointSettings.value } })
  }, 500)
}, { deep: true })
// 全局离地高度是全部航点高度的基准：改动即整体覆盖，之后仍可逐点单独调整。
// 不标记 export_anchor：整体改高不算逐点编辑，不能阻止导出时的航线抽稀。
watch(() => form.altitude_m, (value) => {
  const altitude = Number(value)
  if (!Number.isFinite(altitude) || altitude <= 0 || !waypointSettings.value.length) return
  waypointSettings.value.forEach((setting) => { setting.altitude_m = altitude })
  scheduleWaypointRender(true)
})
const mapAnchorPicker = ref<HTMLElement | null>(null)
const mapAnchorPickerButton = ref<HTMLButtonElement | null>(null)
const mapAnchorMenuOpen = ref(false)
const mapAnchorPickerDisabled = computed(() => checkingPoint.value || mapEditMode.value !== 'route' || activeModule.value === 'photos')
const selectedWaypointIndex = ref<number | null>(null)
const draggingCameraIndex = ref<number | null>(null)
let mapCameraInputsBeforeDrag: boolean | null = null
const suppressNextMapClick = ref(false)
const roadSource = ref<'local' | 'online'>('local')
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
let routeRefreshTimer: ReturnType<typeof setTimeout> | null = null
let isBootstrappingRoute = true

const elapsed = computed(() => preview.value && form.speed_kmh > 0 ? `${(preview.value.distance_km / form.speed_kmh * 60).toFixed(2)} 分钟` : '待计算')
const distance = computed(() => preview.value ? `${preview.value.distance_km.toFixed(2)} km` : '待计算')
const layoutStyle = computed(() => ({ '--sidebar-width': `${sidebarWidth.value}px`, '--insights-width': `${insightsWidth.value}px` }))
const routeKey = computed(() => planningMode.value === 'area'
  ? JSON.stringify(['area', coveragePolygon.value, [...coverageRoadClasses.value].sort(), coverageStartPoint.value, coverageEndPoint.value])
  : JSON.stringify(['anchors', anchors.value.map((anchor) => [anchor.id, anchor.latitude, anchor.longitude]), roadSource.value, form.clearance_m]))
const taskRouteSnapshot = computed(() => ({
  fingerprint: JSON.stringify({ routeKey: routeKey.value, altitude_m: form.altitude_m, speed_kmh: form.speed_kmh }),
  geometry: preview.value?.geometry ?? null,
  road_names: preview.value?.road_names ?? [],
  // 每段的顶点区间与路名：给“积水在路的哪个位置”报路名用。
  segments: preview.value?.segments ?? null,
  data_source: preview.value?.data_source ?? '',
  route_type: planningMode.value === 'area' ? 'area_road_coverage_open'
    : roadSource.value === 'online' ? 'online_driving_reference' : 'road_corridor_demo',
  distance_km: preview.value?.distance_km ?? null,
  anchors: planningMode.value === 'area' && preview.value?.anchors?.length
    ? preview.value.anchors.map(({ id, latitude, longitude }) => ({ id, latitude, longitude }))
    : anchors.value.map(({ id, latitude, longitude }) => ({ id, latitude, longitude })),
  road_source: planningMode.value === 'area' ? 'local' : roadSource.value,
  coverage_area: planningMode.value === 'area' ? {
    polygon: coveragePolygon.value,
    road_classes: [...coverageRoadClasses.value],
    start: coverageStartPoint.value,
    end: coverageEndPoint.value,
    point_anchors: anchors.value.map(({ id, latitude, longitude }) => ({ id, latitude, longitude })),
    metrics: preview.value?.area_coverage ?? null,
  } : null,
  altitude_m: form.altitude_m,
  speed_kmh: form.speed_kmh,
  clearance_m: form.clearance_m,
  waypoint_settings: waypointSettings.value,
}))
const taskRouteMatches = computed(() => {
  if (!preview.value || previewKey.value !== routeKey.value) return false
  const storedFingerprint = activeTask.value?.route.fingerprint
  if (typeof storedFingerprint !== 'string') return false
  try {
    const saved = JSON.parse(storedFingerprint) as { routeKey?: string; altitude_m?: number; speed_kmh?: number }
    const current = JSON.parse(taskRouteSnapshot.value.fingerprint) as { routeKey: string; altitude_m: number; speed_kmh: number }
    return saved.routeKey === current.routeKey && saved.altitude_m === current.altitude_m && saved.speed_kmh === current.speed_kmh
  } catch {
    return false
  }
})
const savedTaskAssessment = computed(() => taskRouteMatches.value && activeTask.value?.weather
  ? activeTask.value.weather as unknown as RouteAssessment : null)
const canFinalizeTask = computed(() => Boolean(preview.value
  && previewKey.value === routeKey.value && !taskBusy.value && !loading.value && !checkingPoint.value && locatingPoint.value === null))
const taskStatusLabel = computed(() => activeTask.value?.status === 'in_flight' ? '飞行中'
  : activeTask.value?.status === 'ready' ? '已准备' : '规划中')
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
    heading: waypointHeading(index), pitch: waypointSettings.value[index]?.gimbal_pitch_angle ?? -90 }
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
const decisionLevel = computed(() => {
  const risk = routeAssessment.value?.weather_risk
  const alerts = risk?.alerts ?? []
  if (risk?.level === 'elevated' || alerts.some(alert => alert.level === 'elevated') || pointWeatherRiskLevel.value === 'elevated') return 'elevated'
  if (risk?.level === 'attention' || alerts.some(alert => alert.level === 'attention') || pointWeatherRiskLevel.value === 'attention') return 'attention'
  if (risk && (risk.level === 'incomplete' || !risk.coverage_complete)) return 'incomplete'
  if (pointWeatherRiskLevel.value === 'incomplete' || (risk && pointWeatherRiskLevel.value !== 'no_trigger')) return 'incomplete'
  if (!risk) return null
  return 'no_trigger'
})
const decisionTone = computed(() => {
  if (decisionLevel.value) return `risk-${decisionLevel.value}`
  return routePhase.value.error || ['blocked', 'failed'].includes(weatherState.value.status) ? 'risk-incomplete' : 'pending'
})
const decisionTitle = computed(() => {
  if (!weatherCoordinates.value) return '先计算道路路线'
  if (decisionLevel.value === 'elevated') return '发现较强天气提醒'
  if (decisionLevel.value === 'attention') return '有天气因素需要留意'
  if (['blocked', 'failed'].includes(weatherState.value.status)) return '天气预测不可用'
  if (routePhase.value.error) return '沿线天气检查未完成'
  if (routePhase.value.pending) return '正在检查沿线天气'
  if (!routeAssessment.value) return weatherJobId.value ? '等待沿线天气结果' : '正在准备天气资料'
  if (decisionLevel.value === 'incomplete') return '天气资料不完整，暂不能判断'
  return '当前规则下没有明显天气提醒'
})
const decisionDescription = computed(() => {
  if (!weatherCoordinates.value) return '先在左侧完成所有主点和飞行参数，系统才能沿路线匹配天气。'
  const risk = routeAssessment.value?.weather_risk
  const hasRouteElevated = risk?.level === 'elevated' || risk?.alerts.some(alert => alert.level === 'elevated')
  const hasRouteAttention = risk?.level === 'attention' || risk?.alerts.some(alert => alert.level === 'attention')
  if (pointWeatherRiskLevel.value === 'elevated' && !hasRouteElevated) {
    return '路线中点参考天气触发较强提醒；该点不代表整条路线，请结合下方沿线天气检查复核。'
  }
  if (pointWeatherRiskLevel.value === 'attention' && !hasRouteElevated && !hasRouteAttention) {
    return '路线中点参考天气有黄色提醒；该点不代表整条路线，请结合下方沿线天气检查复核。'
  }
  if (['blocked', 'failed'].includes(weatherState.value.status)) return weatherState.value.message
  if (routePhase.value.error) return routePhase.value.error
  if (routePhase.value.pending) return '系统正在按出发时间、飞行高度和路线逐点检查天气。'
  if (!routeAssessment.value) return '结果出来后，这里会先告诉你是否有需要留意的天气因素。'
  if (decisionLevel.value === 'no_trigger') return ''
  if (decisionLevel.value === 'incomplete') return '部分路线缺少可用的高度或时间资料，当前判断不完整，不能把“没有提醒”理解为安全。'
  return '较强提醒指标已用红框标出，接近提醒线的指标用橙框标出；可展开沿线天气查看地图点位。这个结果只是天气初筛。'
})
const weatherChecks = computed(() => routeAssessment.value ? weatherOnlyChecks(routeAssessment.value.decision) : [])
const alignmentProgress = computed(() => routeAssessment.value?.coverage.flight_height ? '沿线位置、时间与高度风已匹配'
  : routeAssessment.value?.coverage.surface_space_time ? '地面时空已匹配，高度资料不足' : '等待路线与天气资料匹配')

watch(routeKey, () => {
  clearBatchRange()
  if (planningMode.value === 'area') {
    coverageStartSnapPoint.value = null
    coverageEndSnapPoint.value = null
  }
  if (routeRefreshTimer) clearTimeout(routeRefreshTimer)
  routeRefreshTimer = null
  cancelCalculation()
  error.value = ''
  kmzError.value = ''
  if (previewKey.value !== routeKey.value) {
    preview.value = null
    routeAssessment.value = null
  }
  drawCesiumRoute(false)
  if (isBootstrappingRoute || !routeDefinitionReady.value) return
  routeRefreshTimer = setTimeout(() => {
    routeRefreshTimer = null
    void calculate(false)
  }, 280)
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
  void nextTick(updateCameraPreview)
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

async function chooseBaseMap(next: 'current' | 'satellite') {
  if (baseMap.value === next) return
  pauseFlight()
  cancelCameraPreviewDrag()
  finishMapCameraDrag()
  baseMap.value = next
  await nextTick()
  syncCesiumFlight()
  if (cameraPreviewViewer) cameraPreviewViewer.useDefaultRenderLoop = flightActive.value || (mapEditMode.value === 'node' && selectedWaypointIndex.value !== null)
  updateCameraPreview()
}

function applyCurrentMapView() {
  earthMapRef.value?.setView(mapViewMode.value)
}

function chooseMapViewMode(next: '2d' | '3d') {
  if (mapViewMode.value === next) return
  cancelCameraPreviewDrag()
  finishMapCameraDrag()
  const mode = planningMode.value === 'area' ? areaRouteViewMode : pointRouteViewMode
  mode.value = next
  applyCurrentMapView()
  drawCesiumRoute(false)
  syncCesiumFlight()
}

watch(mapViewMode, () => {
  void nextTick(() => {
    applyCurrentMapView()
    drawCesiumRoute(false)
    syncCesiumFlight()
  })
})

watch([mapEditMode, selectedWaypointIndex], () => {
  cancelCameraPreviewDrag()
  finishMapCameraDrag()
}, { flush: 'sync' })

function revealInvalidSetting(event: Event) {
  const control = event.target as HTMLInputElement
  const group = control.closest<HTMLElement>('[data-settings-page]')
  if (!group || control.getClientRects().length) return
  event.preventDefault()
  settingsPage.value = group.dataset.settingsPage === 'mission' || group.dataset.settingsPage === 'photos' ? group.dataset.settingsPage : 'points'
  void nextTick(() => { control.focus({ preventScroll: true }) })
}

function clearLocationFeedback(target: string) {
  if (locationFeedback.value?.target === target) locationFeedback.value = null
}

function closeNearbyPlacePicker(target?: string) {
  if (target && nearbyPlacePicker.value?.target !== target) return
  nearbyPlacesController?.abort()
  nearbyPlacesController = null
  nearbyPlacePicker.value = null
}

function nearbyPlaceCoordinates(place: PlaceResult) {
  try {
    const point = placeToPoint(place)
    return `纬度 ${point.latitude.toFixed(6)}，经度 ${point.longitude.toFixed(6)}`
  } catch {
    return '坐标暂不可用'
  }
}

function applyNearbyPlace(place: PlaceResult, target: string) {
  if (loading.value || checkingPoint.value || locatingPoint.value) return
  const picker = nearbyPlacePicker.value
  if (!picker || picker.target !== target) return
  let point: Point
  try {
    point = placeToPoint(place)
  } catch {
    locationFeedback.value = { target, message: '该附近地点的坐标无效，请选择其他地点。', error: true }
    return
  }
  const requestVersion = locationRequestVersion
  closeNearbyPlacePicker(target)
  locationFeedback.value = { target, message: `正在检查“${place.name}”是否适合作为该主点…` }
  void chooseMapPoint(point, target).then(() => {
    if (requestVersion !== locationRequestVersion) return
    if (error.value) {
      locationFeedback.value = { target, message: error.value, error: true }
      error.value = ''
    } else {
      locationFeedback.value = { target, message: `已选择“${place.name}”，请在地图上核对点位。` }
    }
  })
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
  closeNearbyPlacePicker(id)
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
  closeNearbyPlacePicker()
  if (!navigator.geolocation) {
    locationFeedback.value = { target, message: '当前页面无法使用定位，请用 HTTPS 或本机地址打开并检查浏览器权限。', error: true }
    return
  }

  const requestVersion = ++locationRequestVersion
  nearbyPlacesController?.abort()
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
      const accuracy = Number.isFinite(coords.accuracy) ? `，定位精度约 ${Math.ceil(coords.accuracy)} 米` : ''
      locationFeedback.value = { target, message: '已获取当前位置，正在读取附近建筑/地点…' }
      const controller = new AbortController()
      nearbyPlacesController = controller
      void fetchLocationAddress(point, controller.signal).then(({ address, nearby_results }) => {
        if (requestVersion !== locationRequestVersion || controller.signal.aborted) return
        nearbyPlacePicker.value = { target, center: point, address, places: nearby_results, accuracy }
        locationFeedback.value = nearby_results.length
          ? { target, message: `已找到 ${nearby_results.length} 个附近建筑/地点，请选择后再检查该主点。` }
          : { target, message: '当前位置附近暂未找到可选建筑/地点，可重新定位或直接填写经纬度。', error: true }
      }).catch((cause) => {
        if (requestVersion !== locationRequestVersion || controller.signal.aborted) return
        locationFeedback.value = { target, message: cause instanceof Error ? cause.message : '读取附近建筑/地点失败，请重试。', error: true }
      }).finally(() => {
        if (requestVersion === locationRequestVersion) nearbyPlacesController = null
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
  if (departureAutoManaged.value) departureBeijing.value = currentDepartureBeijing()
}
function scheduleDepartureClock() {
  refreshDepartureClock()
  const delay = 60_000 - (Date.now() % 60_000)
  departureClockTimer = setTimeout(scheduleDepartureClock, delay)
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
        gimbal_pitch_angle: -90,
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
    heading: waypointHeading(index), pitch: setting?.gimbal_pitch_angle ?? -90 })
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

function applyAllWaypointPitchDown() {
  if (!waypointSettings.value.length || loading.value || checkingPoint.value || kmzLoading.value) return
  waypointSettings.value.forEach((setting) => {
    setting.gimbal_pitch_angle = -90
    setting.export_anchor = true
  })
  waypointEditorFeedback.value = `已将全部 ${waypointSettings.value.length} 个航点设为镜头垂直向下（-90°）。`
  kmzError.value = ''
  scheduleWaypointRender(true)
}

async function openWaypointEditor() {
  if (!waypointSettings.value.length) return
  waypointEditorFeedback.value = ''
  waypointEditorExpanded.value = true
  await nextTick()
  waypointEditorDialog.value?.focus({ preventScroll: true })
}

function closeWaypointEditor() {
  waypointEditorExpanded.value = false
  void nextTick(() => waypointEditorTrigger.value?.focus({ preventScroll: true }))
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
  earthMapRef.value?.focusWaypoint(index)
  return
}

function updateCameraPreview() {
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
    pitch: setting.gimbal_pitch_angle ?? -90,
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

watch([mapEditMode, selectedWaypointIndex, flightActive], async () => {
  await nextTick()
  if (flightActive.value || (mapEditMode.value === 'node' && selectedWaypointIndex.value !== null)) {
    updateCameraPreview()
    if (cameraPreviewViewer) {
      cameraPreviewViewer.useDefaultRenderLoop = true
      cameraPreviewViewer.resize()
      cameraPreviewViewer.scene.requestRender()
    }
  }
  if (cameraPreviewViewer && (!flightActive.value && (mapEditMode.value !== 'node' || selectedWaypointIndex.value === null))) {
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

function djiKmzRequestBody() {
  const route = preview.value
  if (!route) throw new Error('请先计算航线。')
  if (djiModel.value === 'fly' && Number(form.speed_kmh) > 43.2) {
    throw new Error('DJI Fly 航点速度上限 43.2 km/h（12 m/s 设备能力），请先调低巡航速度。')
  }
  return {
    coordinates: route.geometry.coordinates,
    altitude_m: form.altitude_m,
    speed_kmh: form.speed_kmh,
    drone_model: djiModel.value,
    auto_record: kmzAutoRecord.value,
    waypoint_settings: waypointSettings.value,
  }
}

async function rc2ResponseError(response: Response, fallback: string) {
  const payload = await response.json().catch(() => ({}))
  return new Error(typeof payload.detail === 'string' ? payload.detail : fallback)
}

function isRc2DeviceUnavailable(message: string) {
  return /未找到设备|找不到设备|请先解锁\s*RC\s*2|资源管理器可见|device.{0,20}not found|no device/i.test(message)
}

function markRc2DeviceUnavailable() {
  if (rc2Status.value) {
    rc2Status.value = { ...rc2Status.value, available: false, busy: false,
      message: '未检测到已解锁的 DJI RC 2。任务 KMZ 会下载到本机。' }
  }
  rc2Folders.value = []
  rc2TargetName.value = ''
  rc2Job.value = null
  rc2Error.value = ''
}

function refreshRc2Connection(): Promise<void> {
  if (rc2JobRunning.value) return Promise.resolve()
  if (rc2ConnectionPromise) return rc2ConnectionPromise
  const version = ++rc2ConnectionVersion
  rc2Loading.value = true
  rc2Error.value = ''
  const request = (async () => {
    try {
      const statusResponse = await fetch('/api/dji/rc2/status')
      if (!statusResponse.ok) throw await rc2ResponseError(statusResponse, '无法检查 RC 2 连接状态。')
      const status = await statusResponse.json() as Rc2Status
      if (version !== rc2ConnectionVersion) return
      rc2Status.value = status
      if (!status.available) {
        rc2Folders.value = []
        rc2DeviceName.value = ''
        rc2WaypointPath.value = ''
        rc2TargetName.value = ''
        return
      }
      const targetsResponse = await fetch('/api/dji/rc2/targets')
      if (!targetsResponse.ok) throw await rc2ResponseError(targetsResponse, '无法读取 RC 2 上的占位航线。')
      const result = await targetsResponse.json() as { device_name: string; waypoint_path: string; targets: Rc2Target[]; folders?: Rc2FolderTarget[] }
      if (version !== rc2ConnectionVersion) return
      rc2DeviceName.value = result.device_name
      rc2WaypointPath.value = result.waypoint_path
      rc2Folders.value = result.folders ?? []
      if (!rc2Folders.value.some((entry) => entry.folder === rc2TargetName.value)) {
        rc2TargetName.value = rc2Folders.value.length === 1 ? rc2Folders.value[0].folder : ''
      }
    } catch (cause) {
      if (version === rc2ConnectionVersion) {
        if (rc2Status.value) rc2Status.value = { ...rc2Status.value, available: false, message: '' }
        rc2Folders.value = []
        rc2TargetName.value = ''
        rc2Error.value = cause instanceof Error ? cause.message : '读取 RC 2 状态失败。'
      }
    } finally {
      if (version === rc2ConnectionVersion) rc2Loading.value = false
    }
  })()
  rc2ConnectionPromise = request
  void request.finally(() => { if (rc2ConnectionPromise === request) rc2ConnectionPromise = null })
  return request
}

async function startRc2Import(targetFolder: string): Promise<Rc2Job> {
  if (rc2Submitting.value || rc2JobRunning.value) throw new Error('已有 DJI RC 2 导入任务正在运行。')
  const entry = rc2Folders.value.find((item) => item.folder === targetFolder)
  if (!entry) throw new Error('所选占位航线文件夹已不在 RC 2 上；请刷新设备后重试。')
  const kmzRequest = djiKmzRequestBody()
  rc2Submitting.value = true
  rc2Error.value = ''
  rc2Job.value = null
  try {
    const response = await fetch('/api/dji/rc2/import', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...kmzRequest, target_file_name: entry.kmz_name, target_folder: entry.folder }),
    })
    if (!response.ok) throw await rc2ResponseError(response, '无法开始 RC 2 导入任务。')
    const result = await response.json() as { job_id: string; status: 'queued'; message: string; target_file_name: string }
    rc2JobRequestFingerprint.value = JSON.stringify(kmzRequest)
    rc2Job.value = {
      job_id: result.job_id, status: result.status, stage: 'queued', message: result.message,
      target_file_name: result.target_file_name, source_path: null, backup_directory: '', backup_path: null,
      source_sha256: null, readback_sha256: null, remote_state: null,
    }
    return rc2Job.value
  } catch (cause) {
    rc2Error.value = cause instanceof Error ? cause.message : '无法开始 RC 2 导入任务。'
    throw cause
  } finally {
    rc2Submitting.value = false
  }
}

async function waitForRc2Job(jobId: string): Promise<Rc2Job> {
  const deadline = Date.now() + 11 * 60 * 1000
  let pollFailures = 0
  while (!rc2Disposed && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 1500))
    try {
      const response = await fetch(`/api/dji/rc2/jobs/${encodeURIComponent(jobId)}`)
      if (!response.ok) throw await rc2ResponseError(response, '无法读取 RC 2 导入状态。')
      const job = await response.json() as Rc2Job
      if (rc2Job.value?.job_id !== jobId) throw new Error('RC 2 导入任务状态已变化。')
      rc2Job.value = job
      rc2Error.value = ''
      pollFailures = 0
      if (job.status === 'succeeded' || job.status === 'failed') {
        if (job.status === 'succeeded') void refreshRc2Connection()
        return job
      }
    } catch (cause) {
      pollFailures++
      rc2Error.value = cause instanceof Error ? cause.message : '读取 RC 2 导入状态失败。'
      if (pollFailures >= 5) throw cause
    }
  }
  throw new Error(rc2Disposed ? '页面已关闭，无法确认 RC 2 导入结果。' : '等待 RC 2 导入超时。')
}

watch([preview, djiModel], () => {
  if (preview.value && djiModel.value === 'fly' && !rc2Status.value) void refreshRc2Connection()
})

async function calculate(flyToRoute: boolean | SubmitEvent = true) {
  if (loading.value || locatingPoint.value) return
  if (routeRefreshTimer) clearTimeout(routeRefreshTimer)
  routeRefreshTimer = null
  if (!routeDefinitionReady.value) {
    settingsPage.value = 'points'
    if (planningMode.value === 'area') {
      error.value = coveragePolygon.value.length < 3
        ? '请先在地图上框选一个有效的巡查区域。'
        : !coverageEndpointsReady.value
          ? '请先在地图上设置区域起点 A 和终点 B。'
          : '请至少选择一种要覆盖的道路等级。'
    } else {
      const missingIndex = anchors.value.findIndex((anchor) => !validMajorPoint(anchor))
      error.value = missingIndex >= 0 ? `请先填写主点 ${majorPointLabel(missingIndex)} 的有效经纬度。` : '至少需要两个有效主点。'
    }
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
    const areaRequest = planningMode.value === 'area'
      ? {
          polygon: coveragePolygon.value.map(([longitude, latitude]) => ({ longitude, latitude })),
          road_classes: coverageRoadClasses.value,
          start: coverageStartPoint.value,
          end: coverageEndPoint.value,
          endpoint_snap_m: 100,
          altitude_m: form.altitude_m,
          speed_kmh: form.speed_kmh,
        }
      : {
          anchors: anchors.value.map((anchor) => ({ id: anchor.id, latitude: anchor.latitude, longitude: anchor.longitude })),
          altitude_m: form.altitude_m, speed_kmh: form.speed_kmh, clearance_m: form.clearance_m,
          road_source: roadSource.value,
        }
    const response = await fetch(planningMode.value === 'area' ? '/api/routes/area-coverage' : '/api/routes/plan-mission', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(areaRequest),
      signal: controller.signal,
    })
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}))
      throw new Error(typeof payload.detail === 'string' ? payload.detail : '道路路线接口暂时不可用。')
    }
    const result: Preview = await response.json()
    if (controller.signal.aborted || requestVersion !== routeRequestVersion) return
    if (planningMode.value === 'area') {
      if (result.requested_start) coverageStartPoint.value = result.requested_start
      if (result.requested_end) coverageEndPoint.value = result.requested_end
      coverageStartSnapPoint.value = result.snapped_start ?? null
      coverageEndSnapPoint.value = result.snapped_end ?? null
      coverageEndpointTarget.value = null
    }
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

function chooseCoveragePoint(point: Point, endpoint: CoverageEndpoint) {
  if (loading.value || checkingPoint.value) return
  if (!Number.isFinite(point.latitude) || Math.abs(point.latitude) > 90 || !Number.isFinite(point.longitude) || Math.abs(point.longitude) > 180) {
    error.value = '区域端点经纬度无效，请重新选择。'
    return
  }
  const selected = { latitude: Number(point.latitude.toFixed(7)), longitude: Number(point.longitude.toFixed(7)) }
  if (endpoint === 'start') coverageStartPoint.value = selected
  else coverageEndPoint.value = selected
  if (endpoint === 'start') coverageStartSnapPoint.value = null
  else coverageEndSnapPoint.value = null
  coverageEndpointTarget.value = endpoint === 'start' && !coverageEndPoint.value ? 'end' : endpoint === 'end' && !coverageStartPoint.value ? 'start' : null
  error.value = ''
  routeAssessment.value = null
  settingsPage.value = 'points'
  workspacePanel.value = 'map'
}

async function chooseMapPoint(point: Point, target: string = selectedAnchorId.value, fromPlaceSearch = false) {
  if (planningMode.value === 'area' && coverageEndpointTarget.value) {
    chooseCoveragePoint(point, coverageEndpointTarget.value)
    return
  }
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
        if (flatView) viewer.entities.add({
          id: 'selected-view-ground-fill',
          polygon: { hierarchy: new Cesium.CallbackProperty(() => new Cesium.PolygonHierarchy(selectedViewFrustum?.ground ?? []), false),
            height: 0, material: Cesium.Color.fromCssColorString('#72e6c2').withAlpha(0.28) },
        })
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
  const element = mapElement.value
  // The route map is rendered by EarthMap. Keep the legacy Cesium viewer
  // available as a fallback, but do not allocate a second renderer while its
  // host is intentionally hidden.
  if (!Cesium || !element || !element.classList.contains('visible')) return
  try {
    viewer = new Cesium.Viewer(element, {
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
    drawCesiumRoute()
  } catch {
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
      creditContainer: document.createElement('div'),
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
    const tk = import.meta.env.VITE_TIANDITU_TK?.trim()
    cameraPreviewViewer.imageryLayers.addImageryProvider(tk
      ? new Cesium.UrlTemplateImageryProvider({
          url: `https://t{s}.tianditu.gov.cn/DataServer?T=vec_w&x={x}&y={y}&l={z}&tk=${tk}`,
          subdomains: ['0', '1', '2', '3', '4', '5', '6', '7'],
          maximumLevel: 18,
          credit: new Cesium.Credit('© 天地图', true),
        })
      : new Cesium.OpenStreetMapImageryProvider({
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
    const failedViewer = cameraPreviewViewer
    cameraPreviewViewer = null
    if (typeof failedViewer?.destroy === 'function') failedViewer.destroy()
  }
}

onMounted(() => {
  // 登录守卫：主控工作台仅管理员进入；使用者账号自动转去现场处理端。
  void fetchSession().then(current => {
    if (!current) { window.location.replace('/'); return }
    if (current.role === 'field') { window.location.replace('/field'); return }
    session.value = current
  })
  window.addEventListener('resize', handleColumnViewportResize)
  handleColumnViewportResize()
  window.addEventListener('keydown', handleWaypointKeydown)
  window.addEventListener('blur', cancelCameraPreviewDrag)
  window.addEventListener('blur', finishMapCameraDrag)
  window.addEventListener('blur', pauseFlight)
  window.addEventListener('pointerup', endFlightSeek)
  window.addEventListener('pointercancel', endFlightSeek)
  window.addEventListener('pointerup', finishMapCameraDrag)
  window.addEventListener('pointerdown', onMapAnchorOutsidePointerDown)
  scheduleDepartureClock()
  window.addEventListener('focus', refreshDepartureClock)
  void restoreCurrentTask().then(async () => {
    isBootstrappingRoute = false
    await calculate()
    restoreTaskWaypointSettings()
  })
  void refreshMissionTaskList()
  if ((window as any).Cesium) {
    initMap()
  }
  else window.addEventListener('load', initMap, { once: true })
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', handleColumnViewportResize)
  rc2Disposed = true
  rc2ConnectionVersion++
  locationRequestVersion++
  nearbyPlacesController?.abort()
  nearbyPlacesController = null
  nearbyPlacePicker.value = null
  window.removeEventListener('keydown', handleWaypointKeydown)
  window.removeEventListener('blur', cancelCameraPreviewDrag)
  window.removeEventListener('blur', finishMapCameraDrag)
  window.removeEventListener('blur', pauseFlight)
  window.removeEventListener('focus', refreshDepartureClock)
  window.removeEventListener('pointerup', endFlightSeek)
  window.removeEventListener('pointercancel', endFlightSeek)
  window.removeEventListener('pointerup', finishMapCameraDrag)
  window.removeEventListener('pointerdown', onMapAnchorOutsidePointerDown)
  closeMapAnchorMenu()
  cancelCameraPreviewDrag()
  finishMapCameraDrag()
  pauseFlight()
  if (departureClockTimer) clearTimeout(departureClockTimer)
  if (routeRefreshTimer) clearTimeout(routeRefreshTimer)
  if (taskDepartureSaveTimer) clearTimeout(taskDepartureSaveTimer)
  if (taskSettingsSaveTimer) clearTimeout(taskSettingsSaveTimer)
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
      <div class="brand"><span class="brand-mark" aria-hidden="true"><span class="svg-icon icon-route"></span></span><div class="brand-copy"><strong>积水风险智能识别与预警系统</strong><small>航线规划与天气评估</small></div></div>
      <div class="top-actions"><span class="prototype-tag"><span class="svg-icon icon-route" aria-hidden="true"></span>道路参考路线</span><span v-if="session" class="session-badge">管理员 · {{ session.username }}</span><button type="button" class="session-logout" @click="signOut">退出</button></div>
    </header>

    <nav class="workspace-tabs" aria-label="工作区面板">
      <button type="button" :aria-pressed="workspacePanel === 'settings'" aria-controls="mission-sidebar" @click="selectModule('route')">航线设置</button>
      <button type="button" :aria-pressed="workspacePanel === 'map'" aria-controls="route-map" @click="workspacePanel = 'map'">航线地图</button>
      <button type="button" :aria-pressed="workspacePanel === 'overview'" aria-controls="mission-insights" @click="selectModule('status')">任务概览</button>
    </nav>

    <main class="layout module-layout" :style="layoutStyle">
      <aside class="module-rail" aria-label="工作模块">
        <div class="module-rail-label">模块</div>
        <nav class="module-nav">
          <button type="button" class="module-nav-item" :class="{ active: activeModule === 'route' }" :aria-current="activeModule === 'route' ? 'page' : undefined" @click="selectModule('route')">
            <span class="module-nav-icon"><span class="svg-icon icon-route" aria-hidden="true"></span></span><span>1. 选定航线</span>
          </button>
          <button type="button" class="module-nav-item" :class="{ active: activeModule === 'status' }" :aria-current="activeModule === 'status' ? 'page' : undefined" @click="selectModule('status')">
            <span class="module-nav-icon"><span class="svg-icon icon-square-check" aria-hidden="true"></span></span><span>2. 查看天气</span>
          </button>
          <button type="button" class="module-nav-item" :class="{ active: activeModule === 'dji' }" :aria-current="activeModule === 'dji' ? 'page' : undefined" @click="selectModule('dji')">
            <span class="module-nav-icon"><span class="svg-icon icon-download" aria-hidden="true"></span></span><span>3. 命名 / 导出</span>
          </button>
          <button type="button" class="module-nav-item" :class="{ active: activeModule === 'photos' }" :aria-current="activeModule === 'photos' ? 'page' : undefined" @click="selectModule('photos')">
            <span class="module-nav-icon"><span class="svg-icon icon-layers" aria-hidden="true"></span></span><span>4. 任务视频 / 分析</span>
          </button>
        </nav>
        <button type="button" class="module-task-indicator" :class="{ 'is-stale': activeTask && !taskRouteMatches }" :aria-expanded="taskListOpen" @click="toggleTaskList">
          <strong>任务列表</strong>
          <small>{{ activeTask ? `${activeTask.name || taskStatusLabel} · ${activeTask.task_id.slice(-6)}` : `${savedTasks.length} 个已保存` }}</small>
        </button>
      </aside>

      <Teleport to="body">
        <div v-if="taskListOpen" class="task-list-backdrop" @click.self="closeTaskList">
          <section class="task-list-dialog" role="dialog" aria-modal="true" aria-labelledby="saved-task-list-title" tabindex="-1" @keydown.esc.stop.prevent="closeTaskList">
            <header class="task-list-heading">
              <div><h2 id="saved-task-list-title">已保存的任务</h2><small>{{ savedTasks.length }} 个任务 · 选择一个继续</small></div>
              <div><button v-if="savedTasks.length" type="button" class="task-delete-all" :class="{ 'is-armed': pendingDeleteAll }" :disabled="taskListLoading || !!deletingTaskId" @click="requestDeleteAllMissionTasks">{{ deletingTaskId === '__all__' ? '删除中…' : pendingDeleteAll ? '确认全部删除' : '全部删除' }}</button><button type="button" :disabled="taskListLoading" @click="refreshMissionTaskList">{{ taskListLoading ? '刷新中…' : '刷新列表' }}</button><button type="button" aria-label="关闭任务列表" @click="closeTaskList">关闭</button></div>
            </header>
            <p v-if="taskListError" class="task-list-feedback is-error" role="alert">{{ taskListError }}</p>
            <p v-else-if="taskListLoading && !savedTasks.length" class="task-list-feedback" role="status">正在读取已保存的任务…</p>
            <p v-else-if="!savedTasks.length" class="task-list-feedback">还没有保存的任务。完成路线和天气检查后，到“命名 / 导出”阶段创建任务。</p>
            <ul v-else class="saved-task-list">
              <li v-for="task in savedTasks" :key="task.task_id">
                <div class="saved-task-row" :class="{ 'is-current': activeTask?.task_id === task.task_id }">
                  <div class="saved-task-entry" :class="{ 'is-locked': taskBusy || !!deletingTaskId }" role="button" tabindex="0" :aria-disabled="taskBusy || !!deletingTaskId ? 'true' : undefined" @click="openSavedTask(task.task_id)" @keydown.enter.self.prevent="openSavedTask(task.task_id)" @keydown.space.self.prevent="openSavedTask(task.task_id)">
                    <span class="saved-task-entry-title"><strong>{{ task.name || task.route_summary }}</strong><b>{{ activeTask?.task_id === task.task_id ? '当前' : taskStatusText(task.status) }}</b><b v-if="analysisBadge?.taskId === task.task_id" class="is-analysis">识别中 · {{ analysisBadge.text }}</b><b v-else-if="pipelineQueuedTaskIds.includes(task.task_id)" class="is-analysis">识别续跑排队中</b></span>
                    <small>{{ task.route_summary }}</small>
                    <small>{{ taskDateText(task.created_at) }} · {{ task.distance_km === null ? '距离待算' : `${task.distance_km.toFixed(2)} km` }}</small>
                    <small v-if="task.max_wind_m_s !== null || task.max_rain_6h_mm !== null">天气：{{ task.max_wind_m_s === null ? '风缺测' : `最大风 ${task.max_wind_m_s.toFixed(1)} m/s` }} · {{ task.max_rain_6h_mm === null ? '雨缺测' : `六小时雨 ${task.max_rain_6h_mm.toFixed(1)} mm` }}</small>
                    <small>机型 {{ task.drone_model ?? '未选' }} · 关键帧 {{ task.frame_count }} 张{{ task.video_survey_id ? ' · 已导入视频/SRT' : '' }}</small>
                    <button type="button" class="saved-task-delete" :disabled="taskBusy || !!deletingTaskId" :aria-label="`删除任务：${task.name || task.route_summary}`" @click.stop="requestDeleteMissionTask(task.task_id)">{{ deletingTaskId === task.task_id ? '删除中…' : pendingDeleteTaskId === task.task_id ? '取消' : '删除' }}</button>
                  </div>
                  <div v-if="pendingDeleteTaskId === task.task_id" class="saved-task-confirm" role="group" :aria-label="`确认删除任务：${task.name || task.route_summary}`">
                    <span>将永久删除该任务及其视频、关键帧、分析结果和导出文件。</span>
                    <button type="button" class="saved-task-confirm-button" :disabled="!!deletingTaskId" @click="deleteSavedMissionTask(task)">{{ deletingTaskId === task.task_id ? '正在删除…' : '确认删除' }}</button>
                  </div>
                </div>
              </li>
            </ul>
          </section>
        </div>
      </Teleport>

      <Teleport to="body">
        <div v-if="createTaskDialogOpen" class="task-list-backdrop" @click.self="closeCreateTaskDialog">
          <section class="task-create-dialog task-list-dialog" role="dialog" aria-modal="true" aria-labelledby="create-task-title" @keydown.esc.stop.prevent="closeCreateTaskDialog">
            <header class="task-list-heading">
              <div><h2 id="create-task-title">{{ taskFinalizeRetryId ? '继续生成任务航线' : '命名并创建任务' }}</h2><small>确认后创建任务、生成 KMZ，并自动尝试导入 DJI RC 2。</small></div>
              <button type="button" :disabled="taskBusy" aria-label="关闭创建任务窗口" @click="closeCreateTaskDialog">关闭</button>
            </header>
            <form class="task-create-form" @submit.prevent="createCurrentTask">
              <label for="create-task-name">当前任务名称</label>
              <input id="create-task-name" ref="createTaskNameInput" v-model="createTaskName" type="text" maxlength="80" required :disabled="taskBusy" placeholder="例如：下午道路巡检" />
              <small>路线、出发时间、已生成的天气结果和当前机型设置会一并保存。</small>
              <p v-if="taskCreateProgress" class="task-create-progress" role="status" aria-live="polite">{{ taskCreateProgress }}</p>
              <p v-if="taskError" class="form-error" role="alert">{{ taskError }}</p>
              <div class="task-create-actions">
                <button type="button" :disabled="taskBusy" @click="closeCreateTaskDialog">取消</button>
                <button type="submit" class="task-create-confirm" :disabled="taskBusy || !canFinalizeTask">{{ taskBusy ? '正在处理…' : taskFinalizeRetryId ? '继续处理' : '确认创建并导出' }}</button>
              </div>
            </form>
          </section>
        </div>
      </Teleport>

      <Teleport to="body">
        <div v-if="waypointEditorExpanded" class="waypoint-editor-backdrop" @click.self="closeWaypointEditor">
          <section ref="waypointEditorDialog" class="waypoint-editor-dialog" role="dialog" aria-modal="true" aria-labelledby="waypoint-editor-title" tabindex="-1" @keydown.esc.stop.prevent="closeWaypointEditor" @change="markWaypointEdited">
            <header class="waypoint-editor-heading">
              <div><h2 id="waypoint-editor-title">逐点设置高度和面向</h2><small>{{ waypointSettings.length }} 个航点 · 每个航点可单独设置</small></div>
              <button type="button" aria-label="关闭逐点设置" @click="closeWaypointEditor">关闭</button>
            </header>
            <div class="waypoint-editor-body">
              <p class="waypoint-editor-note">高度相对起飞点；机头角度控制水平朝向，真北为 0°。摄像头俯仰默认 -90°（垂直向下），0° 为水平。</p>
              <p v-if="waypointSettings.length > 200" class="waypoint-editor-note">此处列出前 200 个点和当前选中点；完整路线仍保留全部 {{ waypointSettings.length }} 个点。</p>
              <div class="waypoint-editor-bulk-actions" aria-label="批量视角设置">
                <button type="button" :disabled="loading || checkingPoint || kmzLoading" @click="applyAllWaypointPitchDown">全选航点并视角向下</button>
                <small>将全部 {{ waypointSettings.length }} 个航点的摄像头俯仰设为 -90°（垂直向下）</small>
              </div>
              <p v-if="waypointEditorFeedback" class="waypoint-editor-feedback" role="status">{{ waypointEditorFeedback }}</p>
              <div v-for="{ setting, index } in editorWaypoints" :key="index" class="waypoint-row" :data-waypoint-index="index">
                <strong>点 {{ index + 1 }}</strong>
                <label>高度（m）<input v-model.number="setting.altitude_m" type="number" min="1" max="1500" step="1" /></label>
                <label>机头方向<CustomSelect v-model="setting.heading_mode" :options="headingModeOptions" aria-label="机头方向" /></label>
                <label v-if="setting.heading_mode === 'fixed' || setting.heading_mode === 'smoothTransition'">机头角度（°）<input v-model.number="setting.heading_angle" type="number" step="1" /></label>
                <label>摄像头俯仰（°）<input v-model.number="setting.gimbal_pitch_angle" type="number" min="-90" max="45" step="1" /></label>
              </div>
            </div>
            <footer class="waypoint-editor-footer"><button type="button" @click="closeWaypointEditor">完成</button></footer>
          </section>
        </div>
      </Teleport>

      <aside id="mission-sidebar" class="sidebar module-panel" :class="[{ 'panel-active': workspacePanel === 'settings', 'module-active': activeModule === 'route' || activeModule === 'photos' }, `module-mode-${activeModule}`]" aria-label="航线设置">
        <div v-if="activeModule === 'route'" class="section-heading"><span class="eyebrow"><span class="svg-icon icon-route" aria-hidden="true"></span>第 1 步 · 选定航线</span><h1>规划本次飞行</h1><PlaceSearch v-if="planningMode === 'anchors'" :disabled="loading || checkingPoint" :targets="anchorChoices" :selection-feedback="placeSelectionFeedback" @choose-point="chooseSearchedPoint" @clear-selection-feedback="placeSelectionFeedback = null" /></div>

        <form class="route-form" @submit.prevent @invalid.capture="revealInvalidSetting">
          <div v-if="activeModule === 'route'" class="planning-mode-switch" role="group" aria-label="路线规划方式"><button type="button" :class="{ active: planningMode === 'anchors' }" :aria-pressed="planningMode === 'anchors'" @click="choosePlanningMode('anchors')">点到点路线</button><button type="button" :class="{ active: planningMode === 'area' }" :aria-pressed="planningMode === 'area'" @click="choosePlanningMode('area')">区域道路巡查<InfoHint text="路线覆盖本地道路数据里符合筛选等级的中心线，连接段也沿已收录道路规划。道路等级是宽度的近似，不等同于现场实测；覆盖率不包含数据中未收录的道路。起终点会吸附到附近道路，吸附距离超过 100 米或点位在区域外时路线不会生成。" /></button></div>
          <nav v-if="activeModule === 'route'" class="settings-pages" aria-label="航线设置分组"><button type="button" :aria-pressed="settingsPage === 'points'" @click="settingsPage = 'points'">{{ planningMode === 'area' ? '区域与道路' : '主点与坐标' }}</button><button type="button" :aria-pressed="settingsPage === 'mission'" @click="settingsPage = 'mission'">飞行参数</button></nav>
          <div class="settings-fields">
          <div v-if="planningMode === 'anchors'" class="settings-group" data-settings-page="points" :class="{ 'settings-active': settingsPage === 'points' }">
          <div v-for="(anchor, index) in anchors" :key="anchor.id" class="major-point-card" :class="{ 'is-selected': selectedAnchorId === anchor.id, 'is-batch-boundary': batchBoundaryIds.includes(anchor.id), 'is-batch-range': batchAnchorRange && index >= batchAnchorRange.first && index <= batchAnchorRange.last }">
            <div class="point-heading"><input class="batch-boundary-checkbox" type="checkbox" :checked="batchBoundaryIds.includes(anchor.id)" :disabled="!batchRouteAnchorIndices || loading || checkingPoint || (batchBoundaryIds.length === 2 && !batchBoundaryIds.includes(anchor.id))" :aria-label="`选择 ${majorPointLabel(index)} 作为批量视角区间边界`" @change="toggleBatchBoundary(anchor.id)" /><span class="point-mark" :class="index === 0 ? 'point-a' : index === anchors.length - 1 ? 'point-b' : 'point-middle'">{{ majorPointLabel(index) }}</span><div><div class="point-title-row"><strong>{{ majorPointTitle(index) }}</strong><button type="button" class="point-location-button" :disabled="loading || checkingPoint || locatingPoint !== null" :aria-label="locatingPoint === anchor.id ? `正在定位${majorPointTitle(index)} ${majorPointLabel(index)}` : `使用当前位置设置${majorPointTitle(index)} ${majorPointLabel(index)}`" :title="`使用当前位置设置${majorPointTitle(index)} ${majorPointLabel(index)}`" @click="useCurrentLocation(anchor.id)"><span class="svg-icon icon-location-on" aria-hidden="true"></span></button></div></div><button v-if="index === 0 || index === anchors.length - 1" type="button" class="range-toggle" :class="{ active: index === 0 ? showStartRange : showEndRange }" :aria-pressed="index === 0 ? showStartRange : showEndRange" :disabled="loading || checkingPoint" title="显示或隐藏接路搜索范围" @click="toggleRange(index === 0 ? 'start' : 'end')"><span class="svg-icon icon-eye" aria-hidden="true"></span><span>范围：{{ (index === 0 ? showStartRange : showEndRange) ? '开' : '关' }}</span></button></div>
            <div class="coordinate-row"><label>纬度<input v-model.number="anchor.latitude" type="number" step="0.000001" min="-90" max="90" required :disabled="loading" @input="clearLocationFeedback(anchor.id); closeNearbyPlacePicker(anchor.id)" /></label><label>经度<input v-model.number="anchor.longitude" type="number" step="0.000001" min="-180" max="180" required :disabled="loading" @input="clearLocationFeedback(anchor.id); closeNearbyPlacePicker(anchor.id)" /></label></div>
            <p v-if="locationFeedback?.target === anchor.id" class="point-location-feedback" :class="{ 'is-error': locationFeedback.error }" :role="locationFeedback.error ? 'alert' : 'status'">{{ locationFeedback.message }}</p>
            <div v-if="nearbyPlacePicker?.target === anchor.id" class="nearby-place-picker" role="dialog" :aria-label="`选择${majorPointTitle(index)} ${majorPointLabel(index)}附近地点`">
              <div class="nearby-place-picker-head"><strong>附近建筑/地点</strong><button type="button" aria-label="关闭附近地点列表" @click="closeNearbyPlacePicker(anchor.id)">×</button></div>
              <p class="nearby-place-picker-note">已定位到：{{ nearbyPlacePicker.address }}{{ nearbyPlacePicker.accuracy }}。请选择一个地点应用到{{ majorPointLabel(index) }}。</p>
              <div v-if="nearbyPlacePicker.places.length" class="nearby-place-list">
                <button v-for="place in nearbyPlacePicker.places" :key="place.uid || `${place.name}-${place.location.lat}-${place.location.lng}`" type="button" class="nearby-place-option" @click="applyNearbyPlace(place, anchor.id)">
                  <span class="nearby-place-copy"><strong>{{ place.name }}</strong><small v-if="place.address">{{ place.address }}</small><small v-if="typeof place.distance_m === 'number'">距当前位置约 {{ Math.round(place.distance_m) }} 米</small><small>{{ nearbyPlaceCoordinates(place) }}（WGS84）</small></span><span class="nearby-place-arrow" aria-hidden="true">›</span>
                </button>
              </div>
              <p v-else class="nearby-place-empty">没有可选择的附近地点。请关闭列表后重新定位，或手动填写经纬度。</p>
            </div>
            <div v-if="batchBoundaryIds.includes(anchor.id)" class="major-point-actions"><button type="button" :class="{ active: selectedAnchorId === anchor.id }" :aria-pressed="selectedAnchorId === anchor.id" :disabled="checkingPoint || loading" @click="selectedAnchorId = anchor.id; mapEditMode = 'route'">地图选此点</button><button type="button" :disabled="index === 0 || loading || checkingPoint" :aria-label="`上移主点 ${majorPointLabel(index)}`" @click="moveMajorPoint(anchor.id, -1)">上移</button><button type="button" :disabled="index === anchors.length - 1 || loading || checkingPoint" :aria-label="`下移主点 ${majorPointLabel(index)}`" @click="moveMajorPoint(anchor.id, 1)">下移</button><button type="button" :disabled="anchors.length <= 2 || loading || checkingPoint" :aria-label="`删除主点 ${majorPointLabel(index)}`" @click="removeMajorPoint(anchor.id)">删除</button></div>
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

          <div v-else class="settings-group area-coverage-settings" data-settings-page="points" :class="{ 'settings-active': settingsPage === 'points' }">
            <div class="coverage-endpoints" role="group" aria-label="区域巡查起终点">
              <button type="button" :class="{ active: coverageEndpointTarget === 'start' }" :disabled="loading || checkingPoint" @click="chooseMapCoverageEndpoint('start')"><span class="coverage-endpoint-key">A</span><span><strong>区域起点</strong><small v-if="coverageStartPoint">{{ coverageStartPoint.latitude.toFixed(5) }}, {{ coverageStartPoint.longitude.toFixed(5) }}</small></span></button>
              <button type="button" :class="{ active: coverageEndpointTarget === 'end' }" :disabled="loading || checkingPoint" @click="chooseMapCoverageEndpoint('end')"><span class="coverage-endpoint-key">B</span><span><strong>区域终点</strong><small v-if="coverageEndPoint">{{ coverageEndPoint.latitude.toFixed(5) }}, {{ coverageEndPoint.longitude.toFixed(5) }}</small></span></button>
            </div>
            <div class="coverage-road-classes" role="group" aria-label="选择要覆盖的道路等级">
              <label v-for="option in coverageRoadClassOptions" :key="option.value"><input v-model="coverageRoadClasses" type="checkbox" :value="option.value" :disabled="loading" />{{ option.label }}</label>
            </div>
            <div class="coverage-area-actions">
              <button type="button" class="major-point-add" :class="{ 'is-drawing': coverageDrawEnabled }" :disabled="loading" @click="startCoverageDraw">{{ coverageDrawEnabled ? '请在地图上拖动框选区域…' : coveragePolygon.length ? '重新框选道路巡查范围' : '在地图上框选道路巡查范围' }}</button>
              <button v-if="coveragePolygon.length" type="button" class="coverage-area-clear" :disabled="loading" @click="clearCoveragePolygon">清除范围</button>
            </div>
            <p v-if="coveragePolygon.length" class="coverage-area-note">已选 {{ coveragePolygon.length }} 个边界点。可拖动边界点调整范围，双击顶点删除，点击边线中点可增加顶点。</p>
            <div v-if="preview?.area_coverage && previewKey === routeKey" class="coverage-area-summary" aria-live="polite">
              <strong>道路覆盖路线已生成 · {{ preview.area_coverage.route_closed ? '闭环' : 'A → B' }} · {{ (preview.area_coverage.coverage_ratio * 100).toFixed(0) }}% 覆盖</strong>
              <div><span>目标道路</span><b>{{ (preview.area_coverage.target_road_m / 1000).toFixed(2) }} km · {{ preview.area_coverage.road_edge_count }} 段</b></div>
              <div><span>重复经过</span><b>{{ (preview.area_coverage.repeated_road_m / 1000).toFixed(2) }} km</b></div>
              <div><span>连接道路</span><b>{{ (preview.area_coverage.connector_m / 1000).toFixed(2) }} km</b></div>
              <small>路线总长 {{ preview.distance_km.toFixed(2) }} km · 估计 {{ elapsed }} · A/B 吸附 {{ preview.start_snap_m.toFixed(1) }} / {{ preview.end_snap_m.toFixed(1) }} m</small>
            </div>
          </div>

          <div class="settings-group" data-settings-page="mission" :class="{ 'settings-active': settingsPage === 'mission' }">
          <div class="form-line"></div>
          <div class="coordinate-row"><label><span class="label-hint">离地高度 AGL · 米<InfoHint text="目前高度风覆盖 10–100 米；范围外会标记缺测。" /></span><input v-model.number="form.altitude_m" type="number" min="0" max="1000" required :disabled="loading" /></label><label>巡航速度 · km/h<input v-model.number="form.speed_kmh" type="number" min="1" max="200" step="0.5" required :disabled="loading" /></label></div>
          <div v-if="planningMode === 'anchors'" class="clearance-row"><label>建筑缓冲 · 米（演示）<input v-model.number="form.clearance_m" type="number" min="0" max="20" step="0.5" required :disabled="loading || roadSource === 'online'" /></label><span>{{ roadSource === 'local' ? '用于排除靠近建筑的道路段' : '在线机动车路线不使用此参数' }}</span></div>
          <button ref="waypointEditorTrigger" type="button" class="waypoint-editor-trigger" :disabled="!waypointSettings.length" aria-haspopup="dialog" :aria-expanded="waypointEditorExpanded" @click="openWaypointEditor"><span>逐点设置高度和面向</span><small>{{ waypointSettings.length }} 个点</small><span aria-hidden="true">›</span></button>
          </div>

           <div class="settings-group" data-settings-page="photos" :class="{ 'settings-active': settingsPage === 'photos' }">
          <VideoSurveyPanel :task-id="taskRouteMatches ? activeTask?.task_id ?? null : null" :frame-count="taskFrames.length" @track-change="setVideoTelemetryTrack" @position-change="setVideoTelemetryPosition" @targets-change="setVideoCaptureTargets" @frame-capture="addTaskFrame" @frame-saved="onTaskFrameSaved" @survey-change="onTaskSurveyChange" @job-started="onAnalysisJobStarted" @open-queue="analysisQueueOpen = true" />
          <PhotoAnalysisPanel :task-id="taskRouteMatches ? activeTask?.task_id ?? null : null" :frames="taskFrames" :thresholds="waterAlertThresholds" :active-job-id="activeAnalysisJobId" :queue-open="analysisQueueOpen" :pending-review="pendingReviewTargets" :dispatched-count="mapAnalysisTargets.length" @remove-frame="removeTaskFrame" @remove-all-frames="removeAllTaskFrames" @frames-add="addManualTaskFrames" @job-started="onAnalysisJobStarted" @analysis-update="absorbAnalysisResults" @thresholds-change="updateWaterAlertThresholds" @dispatch-review="dispatchAnalysisReview" @manual-confirm="toggleManualConfirm" @open-queue="analysisQueueOpen = true" @close-queue="analysisQueueOpen = false" />
          <div class="analysis-target-tools"><label class="route-export-record"><input v-model="showAnalysisTargets" type="checkbox" />显示已下发积水点（{{ mapAnalysisTargets.length }} 个 · 现场端可见）</label><button type="button" class="weather-refresh" :disabled="!analysisMapTargets.length" @click="clearAnalysisTargets">撤销全部下发</button></div>
          </div>
          </div>
            <div v-if="activeModule === 'route'" class="primary-action-row"><fluent-button class="primary-button" appearance="primary" type="button" :disabled="!weatherCoordinates || loading || checkingPoint || locatingPoint !== null" @click="selectModule('status')"><span class="svg-icon icon-cloud-rain" aria-hidden="true"></span><span>继续查看天气</span><InfoHint text="路线和天气阶段可以继续返回修改；完成后到“命名 / 导出”阶段创建任务。" /></fluent-button></div>
           <p v-if="taskError && activeModule === 'route'" class="form-error" role="alert">{{ taskError }}</p>
           <p v-if="error && activeModule === 'route' && !locationFeedback?.error && !placeSelectionFeedback?.error" class="form-error" role="alert">{{ error }}</p>
        </form>
      </aside>

      <section id="route-map" class="map-panel" :class="{ 'panel-active': workspacePanel === 'map' }" aria-label="航线地图">
        <div ref="mapElement" class="cesium-map" aria-hidden="true"></div>
        <EarthMap ref="earthMapRef" :active="true" :provider="baseMap === 'satellite' ? 'tianditu-sat' : 'osm'" :view-mode="mapViewMode" :start="form.start" :end="form.end" :anchors="mapAnchors" :selected-anchor-id="selectedAnchorId" :route="earthRoute" :coverage-polygon="planningMode === 'area' ? coveragePolygon : []" :coverage-draw-enabled="coverageDrawEnabled" :coverage-edit-enabled="planningMode === 'area' && coveragePolygon.length >= 3" :suppress-map-point-selection="activeModule === 'photos' || (planningMode === 'area' && !coverageEndpointTarget)" :selected-index="selectedWaypointIndex" :edit-mode="mapEditMode" :footprint="earthFootprint" :flight-pose="mapFlightPose" :flight-footprint="flightFootprint" :weather-segments="routeAssessment?.weather_risk.segments ?? []" :weather-samples="showRouteWeatherPoints ? routeAssessment?.samples : null" :weather-risk-points="showRouteWeatherPoints ? routeAssessment?.weather_risk.points : null" :weather-altitude-m="form.altitude_m" :show-start-range="showStartRange" :show-end-range="showEndRange" :range-meters="landingSearchRadiusM" :video-track="activeModule === 'photos' ? videoTelemetryTrack : []" :video-position="activeModule === 'photos' ? videoTelemetryPosition : null" :capture-targets="activeModule === 'photos' ? mapCaptureTargets : []" @select-waypoint="selectWaypoint" @choose-point="chooseMapPoint" @rotate-waypoint="setSelectedWaypointOrientation($event, undefined, true)" @coverage-polygon-created="updateCoveragePolygon" @coverage-polygon-updated="updateCoveragePolygon" />
        <div class="map-top">
          <div v-if="!flightActive" class="map-title-group"><span class="map-badge"><span class="svg-icon icon-layers" aria-hidden="true"></span>{{ (baseMap === 'satellite' ? '卫星底图' : '当前底图') + (mapViewMode === '3d' ? ' · 3D 航线' : ' · 2D 平面') }}</span><span class="map-mode-chip">{{ form.altitude_m }} m AGL</span><div class="map-source-switch base-map-switch" aria-label="底图"><button type="button" :class="{ active: baseMap === 'current' }" :aria-pressed="baseMap === 'current'" @click="chooseBaseMap('current')">当前底图</button><button v-if="tiandituSatelliteAvailable" type="button" :class="{ active: baseMap === 'satellite' }" :aria-pressed="baseMap === 'satellite'" @click="chooseBaseMap('satellite')">卫星底图</button></div><div class="map-edit-switch" aria-label="编辑对象"><button type="button" :class="{ active: mapEditMode === 'route' }" :aria-pressed="mapEditMode === 'route'" @click="mapEditMode = 'route'">路径 / 主点</button><button type="button" :class="{ active: mapEditMode === 'node' }" :aria-pressed="mapEditMode === 'node'" @click="mapEditMode = 'node'">节点 / 视角</button></div></div>
          <button v-if="!flightActive" class="flight-entry" type="button" :disabled="!flightTimeline" aria-label="开始模拟飞行" title="模拟飞行" @click="toggleFlight"><span class="svg-icon icon-drone" aria-hidden="true"></span></button>
          <div v-if="flightActive" class="flight-controls" aria-label="模拟飞行回放">
            <button class="flight-toggle" type="button" :disabled="!flightTimeline" :aria-label="flightPlaying ? '暂停模拟飞行' : flightProgress >= 1 ? '重新播放模拟飞行' : flightActive ? '继续模拟飞行' : '开始模拟飞行'" :aria-pressed="flightPlaying" :title="flightPlaying ? '暂停' : flightProgress >= 1 ? '重播' : flightActive ? '继续' : '模拟飞行'" @click="toggleFlight"><span class="svg-icon icon-drone" aria-hidden="true"></span></button>
            <label class="flight-speed">倍率<CustomSelect v-model="flightSpeed" :options="flightSpeedOptions" aria-label="模拟飞行倍率" :disabled="!flightTimeline" /></label>
            <label class="flight-progress-wrap"><span class="sr-only">模拟飞行进度</span><input class="flight-progress" type="range" min="0" max="1000" step="1" :value="Math.round(flightProgress * 1000)" :disabled="!flightTimeline" :aria-valuetext="`${Math.round(flightProgress * 100)}%，${flightTimeLabel}`" :title="flightTimeLabel" @pointerdown="beginFlightSeek" @pointerup="endFlightSeek" @pointercancel="endFlightSeek" @input="seekFlight" /></label>
            <span class="flight-percent">{{ Math.round(flightProgress * 100) }}%</span>
          </div>
          <button v-if="flightActive" class="flight-exit" type="button" @click="stopFlight">退出模拟飞行</button>
           <span v-if="!flightTimeline" class="map-caption">{{ coverageDrawEnabled ? '拖动地图框选道路巡查区域' : planningMode === 'area' && coverageEndpointTarget ? `点击地图设置区域${coverageEndpointTarget === 'start' ? '起点 A' : '终点 B'}` : planningMode === 'area' ? '拖动边界点调整范围，双击删除，点击边线中点加点' : checkingPoint ? '正在检查点位…' : mapEditMode === 'node' ? '点击节点选中，拖动橙色镜头调整朝向' : `地图点击可设置 ${selectedAnchorChoice?.title} ${selectedAnchorChoice?.label}` }}</span>
        </div>
        <div class="map-view-switch map-source-switch view-mode-switch" aria-label="地图视图"><button type="button" :class="{ active: mapViewMode === '2d' }" :aria-pressed="mapViewMode === '2d'" @click="chooseMapViewMode('2d')">2D 平面</button><button type="button" :class="{ active: mapViewMode === '3d' }" :aria-pressed="mapViewMode === '3d'" @click="chooseMapViewMode('3d')">3D 视角</button></div>
        <div v-if="error" class="map-alert" role="alert">{{ error }}</div>
        <div v-if="activeModule !== 'photos'" class="map-bottom">
          <div ref="mapAnchorPicker" class="map-anchor-picker" :class="{ 'is-open': mapAnchorMenuOpen }">
            <span class="svg-icon icon-map-pin" aria-hidden="true"></span>
            <span class="map-anchor-label">{{ planningMode === 'area' ? '规划模式' : '地图选点' }}</span>
            <button ref="mapAnchorPickerButton" class="map-anchor-trigger" type="button" :disabled="mapAnchorPickerDisabled" :aria-expanded="mapAnchorMenuOpen" aria-haspopup="listbox" aria-controls="map-anchor-options" @click="toggleMapAnchorMenu" @keydown="onMapAnchorPickerKeydown">
              <span>{{ planningMode === 'area' ? coverageEndpointPickerLabel : `${selectedAnchorChoice?.label} · ${selectedAnchorChoice?.title}` }}</span>
              <span class="map-anchor-chevron" aria-hidden="true"></span>
            </button>
            <Transition name="map-anchor-menu">
              <div v-if="mapAnchorMenuOpen" id="map-anchor-options" class="map-anchor-options" role="listbox" aria-label="选择地图规划模式">
                <template v-if="planningMode === 'anchors'">
                  <button v-for="choice in anchorChoices" :key="choice.id" class="map-anchor-option" :class="{ selected: choice.id === selectedAnchorId }" type="button" role="option" :aria-selected="choice.id === selectedAnchorId" @click="chooseMapAnchor(choice.id)">
                    <span class="map-anchor-option-key">{{ choice.label }}</span>
                    <span>{{ choice.title }}</span>
                    <span v-if="choice.id === selectedAnchorId" class="map-anchor-check" aria-hidden="true">✓</span>
                  </button>
                </template>
                <template v-if="planningMode === 'area'">
                  <button class="map-anchor-option" :class="{ selected: coverageEndpointTarget === 'start' }" type="button" role="option" :aria-selected="coverageEndpointTarget === 'start'" @click="chooseMapCoverageEndpoint('start')">
                    <span class="map-anchor-option-key">A</span><span>区域起点</span><span v-if="coverageEndpointTarget === 'start'" class="map-anchor-check" aria-hidden="true">✓</span>
                  </button>
                  <button class="map-anchor-option" :class="{ selected: coverageEndpointTarget === 'end' }" type="button" role="option" :aria-selected="coverageEndpointTarget === 'end'" @click="chooseMapCoverageEndpoint('end')">
                    <span class="map-anchor-option-key">B</span><span>区域终点</span><span v-if="coverageEndpointTarget === 'end'" class="map-anchor-check" aria-hidden="true">✓</span>
                  </button>
                </template>
                <button v-if="planningMode === 'anchors'" class="map-anchor-option" type="button" role="option" aria-selected="false" @click="chooseMapAreaMode">
                  <span class="map-anchor-option-key">区</span>
                  <span>区域划分模式</span>
                </button>
                <button v-else class="map-anchor-option selected" type="button" role="option" aria-selected="true" @click="keepCoverageMode">
                  <span class="map-anchor-option-key">区</span><span>区域划分模式</span><span class="map-anchor-check" aria-hidden="true">✓</span>
                </button>
              </div>
            </Transition>
          </div>
          <span v-if="planningMode === 'area'" class="coverage-map-hint">{{ coveragePolygon.length }} 个边界点 · {{ coverageRoadClasses.length }} 类道路</span>
          <span class="map-radius"><span class="svg-icon icon-map-pin" aria-hidden="true"></span>{{ planningMode === 'area' ? '区域道路覆盖' : `接路判定 ${landingSearchRadiusM} m` }}</span>
          <span class="map-scale"><span class="svg-icon icon-wind" aria-hidden="true"></span>任务高度 {{ form.altitude_m }} m</span>
        </div>
        <div v-if="!flightActive && mapEditMode === 'node' && selectedWaypointIndex !== null && selectedWaypointSetting()" class="node-controller" aria-label="航点控制器"><div class="node-controller-head"><strong>节点 {{ selectedWaypointIndex + 1 }} · 视角控制</strong><span>{{ Math.round(waypointHeading(selectedWaypointIndex)) }}° · {{ selectedWaypointSetting()?.altitude_m ?? form.altitude_m }}m</span></div><div class="node-controller-body"><div class="wasd-pad" aria-label="WASD 平面移动控制"><button type="button" @click="moveSelectedWaypoint(0, 1)">W</button><button type="button" @click="moveSelectedWaypoint(-1, 0)">A</button><button type="button" @click="moveSelectedWaypoint(0, -1)">S</button><button type="button" @click="moveSelectedWaypoint(1, 0)">D</button></div><div class="node-dial"><button type="button" aria-label="逆时针旋转" @click="rotateSelectedWaypoint(-15)">↺</button><strong>{{ Math.round(waypointHeading(selectedWaypointIndex)) }}°</strong><button type="button" aria-label="顺时针旋转" @click="rotateSelectedWaypoint(15)">↻</button></div><div class="node-axis"><button type="button" @click="rotateSelectedWaypoint(0, 5)">镜头上</button><button type="button" @click="rotateSelectedWaypoint(0, -5)">镜头下</button><button type="button" @click="adjustSelectedWaypoint({ altitude_m: Math.min(1500, (selectedWaypointSetting()?.altitude_m ?? form.altitude_m) + 5) })">高度 +</button><button type="button" @click="adjustSelectedWaypoint({ altitude_m: Math.max(1, (selectedWaypointSetting()?.altitude_m ?? form.altitude_m) - 5) })">高度 −</button></div></div><small>W/S 平面前后 ±1m，A/D 平面左右 ±1m，Q/E 航向 ±5°，Z/C 俯仰 ±5°可直接调整水平朝向。</small></div>
        <div v-if="routeAssessment" class="weather-map-legend">沿线初筛：蓝 · 未触发，橙 · 关注，红 · 较强，灰 · 缺测<br />基于 0.25° 场，不是街道级风险，也不代表飞行许可。</div>
        <div class="camera-preview" :class="{ visible: (flightActive || (mapEditMode === 'node' && selectedWaypointIndex !== null)) && cameraPreviewReady }" aria-label="当前节点相机视角预览"><div ref="cameraPreviewElement" class="camera-preview-map" :data-base-map="baseMap" @pointerdown.capture="handleCameraPreviewPointerDown" @pointermove="handleCameraPreviewPointerMove" @pointerup="handleCameraPreviewPointerEnd" @pointercancel="handleCameraPreviewPointerEnd" @lostpointercapture="handleCameraPreviewPointerEnd"><div ref="cesiumCameraElement" :style="{ position: 'absolute', inset: '0' }"></div></div><div class="camera-preview-head"><strong>当前视角</strong><span v-if="flightActive">{{ flightNodeLabel }} / {{ Math.round(flightSample?.pitch ?? 0) }}&deg;</span><span v-else-if="selectedWaypointIndex !== null">节点 {{ selectedWaypointIndex + 1 }} · {{ Math.round(selectedWaypointSetting()?.gimbal_pitch_angle ?? -90) }}°</span></div><small>地图模拟视角 · 不是实时相机视频</small></div>
        <div v-if="loading" class="map-loading-overlay">
          <div class="map-loading-card">
            <button type="button" class="map-loading-cancel" aria-label="取消路线计算" title="取消" @click="cancelCalculation">×</button>
            <span class="map-loading-spinner" aria-hidden="true"></span>
            <strong role="status" aria-live="polite">加载中…</strong>
            <span>{{ roadSource === 'online' ? '正在获取在线道路路线' : '正在计算本地道路路线' }}</span>
          </div>
        </div>
      </section>

      <div class="column-resizer column-resizer-left" role="separator" aria-orientation="vertical" aria-label="调整航线设置栏宽度" aria-controls="mission-sidebar route-map" :aria-valuenow="Math.round(sidebarWidth)" aria-valuemin="260" :aria-valuemax="Math.round(widthLimit('left'))" tabindex="0" title="拖动调整航线设置栏宽度，双击恢复默认" :style="{ left: `calc(76px + ${sidebarWidth}px)` }" @pointerdown="startColumnResize('left', $event)" @dblclick="resetColumnWidths" @keydown="resizeColumnByKeyboard('left', $event)"></div>
      <div class="column-resizer column-resizer-right" role="separator" aria-orientation="vertical" aria-label="调整任务概览栏宽度" aria-controls="route-map mission-insights" :aria-valuenow="Math.round(insightsWidth)" :aria-valuemin="minimumWidth('right')" :aria-valuemax="Math.round(widthLimit('right'))" tabindex="0" title="拖动调整任务概览栏宽度，双击恢复默认" :style="{ right: `${insightsWidth}px` }" @pointerdown="startColumnResize('right', $event)" @dblclick="resetColumnWidths" @keydown="resizeColumnByKeyboard('right', $event)"></div>

      <aside id="mission-insights" class="insights module-panel" :class="[{ 'panel-active': workspacePanel === 'overview', 'module-active': activeModule === 'status' || activeModule === 'dji' }, `module-mode-${activeModule}`]" aria-label="任务概览" tabindex="0" @change="markWaypointEdited">
        <div v-if="activeModule === 'status'" class="insights-header"><span class="eyebrow"><span class="svg-icon icon-square-check" aria-hidden="true"></span>第 2 步 · 查看天气</span><h2>检查当前航线天气<InfoHint text="天气阶段的结果会在最后创建任务时一并保存。" /></h2></div>
        <div v-if="activeModule === 'dji'" class="insights-header"><span class="eyebrow"><span class="svg-icon icon-download" aria-hidden="true"></span>第 3 步 · 命名与机型导出</span><h2>导出本次任务航线</h2></div>
        <p v-if="taskError && (activeModule === 'status' || activeModule === 'dji')" class="form-error" role="alert">{{ taskError }}</p>
        <section v-if="activeModule === 'status'" class="overview-hero" aria-label="天气结论概览">
        <div class="mission-facts"><div><span class="fact-label"><span class="svg-icon icon-route" aria-hidden="true"></span>路线长度</span><strong>{{ distance }}</strong></div><div><span class="fact-label"><span class="svg-icon icon-refresh" aria-hidden="true"></span>预计用时</span><strong>{{ elapsed }}</strong></div><div><span class="fact-label"><span class="svg-icon icon-map-pin" aria-hidden="true"></span>天气起算</span><strong>{{ departureBeijing.replace('T', ' ') }}</strong><small>按当前北京时间自动刷新</small></div></div>
         <div class="decision" :class="decisionTone" aria-live="polite"><span class="decision-kicker"><span class="svg-icon icon-alert" aria-hidden="true"></span>气象初筛<InfoHint text="这里只反映已覆盖的天气指标，不等于放飞许可。" /></span><strong>{{ decisionTitle }}</strong><p v-if="decisionDescription">{{ decisionDescription }}</p><details v-if="routeAssessment"><summary><span class="svg-icon icon-eye" aria-hidden="true"></span>查看判断依据</summary><div v-for="check in weatherChecks" :key="check.key" class="decision-check"><b>{{ check.label }}</b><span :class="check.state === 'ready' ? 'check-ready' : 'check-pending'">{{ check.state === 'ready' ? '资料已就绪' : check.state === 'no_trigger' ? '未触发当前阈值' : check.state === 'elevated' ? '较强提示' : check.state === 'attention' ? '需要留意' : '资料待补齐' }}</span><small>{{ check.detail }}</small></div></details></div>
        </section>
         <details v-if="activeModule === 'status'" class="overview-group" open>
           <summary><span><span class="svg-icon icon-cloud-rain" aria-hidden="true"></span>沿线天气检查</span><small>风、降雨、阵风和能见度</small></summary>
          <div class="overview-group-body overview-weather">
            <WeatherPanels v-if="allAnchorsReady" :key="weatherRefreshVersion" :altitude-m="form.altitude_m" :start="form.start" :end="form.end" @job-ready="weatherJobId = $event" @state-changed="weatherState = $event" @point-risk-changed="pointWeatherRiskLevel = $event">
              <template #route-weather>
                <RouteWeatherPanel :job-id="weatherJobId" :coordinates="weatherCoordinates" :departure-beijing="departureBeijing" :speed-kmh="form.speed_kmh" :altitude-m="form.altitude_m" :limits="activeLimits" :saved-assessment="savedTaskAssessment" @assessed="recordTaskWeather" @phase="routePhase = $event" @limits-change="applyWeatherLimits" />
              </template>
            </WeatherPanels>
            <RouteWeatherPanel v-else :job-id="weatherJobId" :coordinates="weatherCoordinates" :departure-beijing="departureBeijing" :speed-kmh="form.speed_kmh" :altitude-m="form.altitude_m" :limits="activeLimits" :saved-assessment="savedTaskAssessment" @assessed="recordTaskWeather" @phase="routePhase = $event" @limits-change="applyWeatherLimits" />
          </div>
        </details>
          <details v-if="activeModule === 'status'" class="overview-group"><summary><span><span class="svg-icon icon-layers" aria-hidden="true"></span>天气规则和技术状态</span><small>仅在需要核对时查看</small></summary><div class="overview-group-body"><p class="weather-threshold-note">中点温度、地面风、六小时降雨，以及沿线飞行高度风、侧风和地面能见度的报警阈值已移至对应天气卡片配置。未列出的逆风和阵风继续使用演示阈值；这些提示不代表放飞许可。</p><div class="pipeline"><h3>系统状态</h3><div><span>01</span><p>{{ alignmentProgress }}</p></div><div><span>02</span><p>{{ routeAssessment ? decisionTitle : '沿线初筛等待资料' }}</p></div><div><span>03</span><p>{{ routePhase.pending ? '正在匹配沿线天气' : routePhase.error || (routeAssessment ? '沿线天气已匹配' : '沿线初筛等待资料') }}</p></div></div></div></details>
          <div v-if="activeModule === 'dji' && preview" class="road-summary">
            <strong><span class="svg-icon icon-route" aria-hidden="true"></span>当前路线：{{ preview.road_names.join(' → ') || '未命名道路' }}</strong>
            <span>{{ distance }} · 预计用时 {{ elapsed }}</span>
            <details v-if="!preview.area_coverage"><summary><span class="svg-icon icon-eye" aria-hidden="true"></span>查看路线数据</summary><small>直线 {{ preview.straight_distance_km.toFixed(2) }} km · {{ preview.building_clearance_m === null ? '在线模式未检查建筑缓冲' : `建筑缓冲 ${preview.building_clearance_m} m` }}</small><small>首末主点到道路分别约 {{ preview.start_snap_m }} m / {{ preview.end_snap_m }} m</small><small>手动主点 {{ anchors.length }} 个 · 自动生成小点 {{ generatedPointCount }} 个 · {{ preview.segments?.length ?? 1 }} 段</small><small v-for="segment in preview.segments" :key="segment.id">{{ majorPointLabel(segment.index) }} → {{ majorPointLabel(segment.index + 1) }}：{{ segment.distance_km.toFixed(2) }} km</small><small>{{ preview.data_source }}</small><small>路线数据 © <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap contributors</a></small></details>
            <div v-else class="coverage-route-detail"><strong>区域道路覆盖 · {{ preview.area_coverage.route_closed ? '闭环' : 'A → B' }}</strong><span>目标道路 {{ (preview.area_coverage.target_road_m / 1000).toFixed(2) }} km · 重复经过 {{ (preview.area_coverage.repeated_road_m / 1000).toFixed(2) }} km</span><span>连接道路 {{ (preview.area_coverage.connector_m / 1000).toFixed(2) }} km · 道路等级 {{ preview.area_coverage.road_classes.join('、') }}</span><small>{{ preview.data_source }} · A/B 吸附 {{ preview.start_snap_m.toFixed(1) }} / {{ preview.end_snap_m.toFixed(1) }} m。</small></div>
            <div class="route-export"><label><span class="label-hint">DJI 机型模板<InfoHint :text="`创建任务时会生成并归档 KMZ。按相对起飞点高度生成，路线折点会先抽稀，减少 DJI 中高度标签和航点标记重叠。${preview.area_coverage ? '此路线用于道路区域巡查；起点是道路中心线参考点，仍需现场选择合适起降位置。' : roadSource === 'online' ? '当前是在线机动车道路参考，不应直接作为飞行线。' : '当前是道路走廊参考，不代表已核实飞行航线。'}${djiModel === 'fly' ? 'DJI Fly 需先创建一条占位航线，再用相同 UUID 文件名替换 waypoint 目录中的 KMZ；Air 3/Air 3S 使用 DJI Fly 消费级 WPML（uav.com 命名空间、几何写入 waylines.wpml、云台俯仰用 waypointGimbalPitchAngle），航点速度上限 43.2 km/h（12 m/s 设备能力）。' : '导入 DJI Pilot 2 后仍需核对机型、动作、返航、空域和现场条件。'}文件不是放飞许可。`" /></span><CustomSelect v-model="djiModel" :options="djiModelOptions" aria-label="DJI 机型模板" /></label><label class="route-export-record"><input v-model="kmzAutoRecord" type="checkbox" />自动录像 · 到首航点开始、末航点停止（结束自动返航）</label><button type="button" class="weather-refresh" :disabled="!flightTimeline" @click="toggleFlight"><span class="svg-icon icon-drone" aria-hidden="true"></span>{{ flightPlaying ? '暂停巡航动画' : flightProgress >= 1 ? '重播巡航动画' : flightActive ? '继续巡航动画' : '查看巡航路线动画' }}</button><button type="button" class="weather-refresh task-flight-start" :disabled="!activeTask || !taskRouteMatches || !activeTask.export_file_name || activeTask.status === 'in_flight'" @click="markTaskFlightStarted">{{ activeTask?.status === 'in_flight' ? '已标记本次任务开始' : '标记本次任务已开始' }}</button><small v-if="activeTask?.export_file_name">KMZ 已保存在任务目录：{{ activeTask.export_file_name }}</small><small v-if="kmzError" class="form-error" role="alert">{{ kmzError }}</small><small v-if="kmzNotice" class="task-create-notice" role="status">{{ kmzNotice }}</small></div>
          </div>
         <section v-if="activeModule === 'dji' && ((preview && djiModel === 'fly') || rc2Job)" class="rc2-import-panel" aria-label="导入 DJI RC 2">
          <div class="rc2-import-heading">
            <h3>导入已连接的 DJI RC 2<InfoHint text="创建任务时会自动检查 RC 2 并尝试导入；无法确认可替换目标时会下载 KMZ。此操作只适用于 DJI Fly / RC 2，导入完成后仍需在遥控器上打开航线，核对航点与动作。" /></h3>
            <button type="button" :disabled="rc2Loading || rc2JobRunning" @click="refreshRc2Connection">{{ rc2Loading ? '检测中…' : '检测 / 刷新设备' }}</button>
          </div>
          <p v-if="rc2Loading || rc2Status?.message" role="status">{{ rc2Loading ? '正在检查本机和遥控器…' : rc2Status?.message }}</p>
          <p v-if="rc2Error" class="form-error" role="alert">{{ rc2Error }}</p>
          <template v-if="preview && djiModel === 'fly' && rc2Status?.available">
            <small v-if="rc2DeviceName">遥控器：{{ rc2DeviceName }}</small>
            <small v-if="rc2WaypointPath">占位航线目录：{{ rc2WaypointPath }}</small>
            <label>选择已有占位航线文件夹
              <CustomSelect v-model="rc2TargetName" :options="rc2TargetOptions" aria-label="选择已有占位航线文件夹" :disabled="rc2Loading || rc2JobRunning || !rc2Folders.length" />
            </label>
            <small v-if="!rc2Folders.length && !rc2Loading">没有可替换的占位航线文件夹。需要 RC 2 上存在 waypoint/&lt;编号&gt;/ 文件夹（DJI Fly 占位航线），且里面有与文件夹同名的 .kmz；创建任务时会下载 KMZ 文件。</small>
            <small v-else-if="rc2Folders.length > 1 && !rc2TargetName">请选择要替换的占位航线文件夹；未选择时创建任务会下载 KMZ 文件。</small>
          </template>
          <div v-if="rc2Job" class="rc2-job" :class="{ 'rc2-job-failed': rc2Job.status === 'failed', 'rc2-job-done': rc2Job.status === 'succeeded' }" role="status" aria-live="polite">
            <strong>{{ !rc2JobMatchesCurrent ? '上次航线的' : '' }}{{ rc2Job.status === 'succeeded' ? '导入并回读校验完成' : rc2Job.status === 'failed' ? '导入失败' : '导入进行中' }}</strong>
            <small v-if="!rc2JobMatchesCurrent">当前路线或航点设置与提交任务时不同；以下结果对应提交时的版本。</small>
            <span>目标：{{ rc2Job.target_file_name }}</span>
            <ol class="rc2-job-steps">
              <li :class="{ active: rc2Job.stage === 'generating', done: !!rc2Job.source_path }">生成 KMZ</li>
              <li :class="{ active: rc2Job.stage === 'uploading', done: rc2Job.stage === 'verified' }">备份并传输</li>
              <li :class="{ active: rc2Job.stage === 'verified', done: rc2Job.status === 'succeeded' }">回读校验</li>
            </ol>
          </div>
        </section>
        <div v-if="activeModule === 'dji'" class="task-create-footer">
          <p v-if="taskCreateNotice" class="task-create-notice" role="status">{{ taskCreateNotice }}</p>
          <fluent-button class="primary-button" appearance="primary" type="button" :disabled="!canFinalizeTask" @click="openCreateTaskDialog"><span class="svg-icon" :class="taskBusy ? 'icon-refresh is-spinning' : 'icon-square-check'" aria-hidden="true"></span><span>创建任务</span></fluent-button>
        </div>
      </aside>
    </main>
  </div>
</template>

<style scoped>
 .route-form .settings-fields { overflow-y: auto; overscroll-behavior: contain; padding-right: 5px; scrollbar-width: none; -ms-overflow-style: none; }
 .route-form .settings-fields::-webkit-scrollbar { display: none; width: 0; height: 0; }
 .planning-mode-switch { display: grid; grid-template-columns: 1fr 1fr; gap: 4px; margin: 2px 0 8px; padding: 4px; border: 1px solid #dbe3ea; border-radius: 8px; background: #f3f6f8; }
 .planning-mode-switch button { min-height: 31px; padding: 5px 8px; border: 1px solid transparent; border-radius: 6px; background: transparent; color: #596b7c; font-size: 12px; font-weight: 600; cursor: pointer; }
 .planning-mode-switch button.active { border-color: #b7d4eb; background: #fff; color: #1265a7; box-shadow: 0 1px 2px rgb(15 23 42 / .08); }
 .coverage-road-classes { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 8px; margin: 10px 0; }
 .coverage-road-classes label { display: flex; align-items: center; gap: 6px; min-height: 28px; color: #394b5e; font-size: 12px; }
 .coverage-road-classes input { width: 15px; height: 15px; margin: 0; accent-color: var(--ui-accent); }
 .coverage-endpoints { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 7px; margin: 10px 0 4px; }
 .coverage-endpoints button { display: flex; align-items: center; gap: 7px; min-width: 0; min-height: 48px; padding: 7px 8px; border: 1px solid #d7e1ea; border-radius: 7px; background: #fff; color: #344455; text-align: left; cursor: pointer; }
 .coverage-endpoints button:hover:not(:disabled) { border-color: #88bcea; background: #f5faff; }
 .coverage-endpoints button.active { border-color: #4f9bd7; background: #eaf5fd; color: #1265a7; }
 .coverage-endpoints button:disabled { cursor: default; opacity: .55; }
 .coverage-endpoint-key { display: grid; flex: none; width: 24px; height: 24px; place-items: center; border-radius: 50%; background: #1769aa; color: #fff; font-size: 12px; font-weight: 800; }
 .coverage-endpoints button:nth-child(2) .coverage-endpoint-key { background: #bd5a2b; }
 .coverage-endpoints button > span:last-child { display: grid; min-width: 0; gap: 1px; }
 .coverage-endpoints strong, .coverage-endpoints small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
 .coverage-endpoints strong { font-size: 12px; }
 .coverage-endpoints small { color: var(--ui-muted); font-size: 12px; }
 .coverage-area-actions { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 7px; }
 .coverage-area-actions .major-point-add { margin-top: 0; }
 .coverage-area-actions .major-point-add.is-drawing { border-color: #0f6cbd; background: #e9f4fd; }
 .coverage-area-clear { min-height: 36px; padding: 6px 9px; border: 1px solid #d7dfe8; border-radius: 6px; background: #fff; color: #526273; font-size: 12px; cursor: pointer; }
 .coverage-area-note { margin: 8px 0; color: var(--ui-muted); font-size: 12px; line-height: 1.5; }
 .coverage-area-summary { display: grid; gap: 7px; margin-top: 12px; padding: 10px; border: 1px solid #cfe0ee; border-radius: 8px; background: #f7fbff; color: #344455; font-size: 12px; }
 .coverage-area-summary > strong { color: #1769aa; font-size: 12px; }
 .coverage-area-summary > div { display: flex; justify-content: space-between; gap: 8px; }
 .coverage-area-summary > div span, .coverage-area-summary small { color: var(--ui-muted); }
 .coverage-map-hint { padding: 8px 10px; border: 1px solid #b7d4eb; border-radius: 7px; background: #fff; color: #1769aa; font-size: 12px; }
 .coverage-route-detail { display: grid; gap: 4px; margin: 6px 0; padding: 9px 10px; border: 1px solid #cfe0ee; border-radius: 7px; background: #f7fbff; color: #344455; font-size: 12px; }
 .coverage-route-detail strong { color: #1769aa; font-size: 12px; }
 .coverage-route-detail small { color: var(--ui-muted); line-height: 1.45; }
.major-point-card { container-type: inline-size; padding: 12px 4px; border-bottom: 1px solid var(--ui-line); }
.major-point-card.is-batch-range { background: #f0faf9; }
.major-point-card.is-selected { background: #f8fbff; }
.major-point-card .point-heading { margin-bottom: 8px; }
.batch-boundary-checkbox { flex: none; width: 20px; height: 20px; margin: 0; accent-color: #168782; cursor: pointer; }
.batch-boundary-checkbox:disabled { cursor: default; opacity: .5; }
.major-point-card .point-mark { width: 28px; flex: none; }
.major-point-card .point-middle, .major-point-card .point-middle + div .point-location-button { color: #197b78; }
.major-point-actions { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 8px; }
.major-point-actions button { min-height: 26px; padding: 3px 6px; border: 1px solid #dbe2ea; border-radius: 6px; background: #fff; color: #526273; font-size: 12px; cursor: pointer; }
.major-point-actions button.active { border-color: #88bcea; background: #e8f3fc; color: #0f6cbd; font-weight: 700; }
.major-point-actions button:hover:not(:disabled) { border-color: #88bcea; color: #0f6cbd; }
.major-point-actions button:disabled { opacity: .45; cursor: default; }
.nearby-place-picker { margin-top: 8px; padding: 9px; border: 1px solid #b8d7ee; border-radius: 8px; background: #f5faff; }
.nearby-place-picker-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; color: #1769aa; font-size: var(--font-caption); }
.nearby-place-picker-head button { width: 24px; height: 24px; padding: 0; border: 0; border-radius: 5px; background: transparent; color: #6b7b8d; font-size: 20px; line-height: 1; }
.nearby-place-picker-head button:hover { background: #e5f1fb; }
.nearby-place-picker-note { margin: 4px 0 7px; color: #617186; font-size: 12px; line-height: 1.45; }
.nearby-place-list { display: grid; gap: 5px; max-height: 230px; overflow-y: auto; }
.nearby-place-option { display: flex; align-items: center; gap: 7px; width: 100%; min-width: 0; padding: 7px 8px; border: 1px solid #d6e5f1; border-radius: 7px; background: #fff; color: #243447; text-align: left; }
.nearby-place-option:hover { border-color: #6eaed8; background: #f0f8ff; }
.nearby-place-copy { display: grid; min-width: 0; gap: 2px; flex: 1; }
.nearby-place-copy strong, .nearby-place-copy small { overflow-wrap: anywhere; }
.nearby-place-copy strong { font-size: 12px; }
.nearby-place-copy small { color: #617186; font-size: 12px; line-height: 1.35; }
.nearby-place-arrow { flex: none; color: #1769aa; font-size: 20px; }
.nearby-place-empty { margin: 4px 0 0; color: #875100; font-size: 12px; line-height: 1.45; }
.major-point-add { display: block; width: 100%; min-height: 38px; margin-top: 12px; border: 1px dashed #83b5df; border-radius: 8px; background: #f2f8fe; color: #0f6cbd; font-size: 13px; font-weight: 700; cursor: pointer; }
.major-point-add:hover:not(:disabled) { background: #e6f1fb; }
.major-point-add:disabled { opacity: .5; cursor: default; }
.batch-camera-panel { margin-top: 12px; padding: 12px; border: 1px solid #b8dedb; border-radius: 8px; background: #f7fffe; box-shadow: 0 4px 16px #172b4d1c; }
.batch-camera-panel > strong, .batch-camera-panel > small { display: block; }
.batch-camera-panel > strong { color: #0b625f; font-size: var(--font-body); }
.batch-camera-panel > small { margin-top: 3px; color: var(--ui-muted); font-size: 12px; line-height: 1.4; }
.batch-camera-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin-top: 10px; }
.batch-camera-fields label { min-width: 0; color: #344455; font-size: 12px; font-weight: 600; }
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
.map-anchor-picker.is-open { border-color: #8bbbe2; box-shadow: 0 0 0 3px #dcecf9, 0 8px 20px #17324d1c; }
.map-anchor-label { color: #526273; }
.map-anchor-trigger { display: inline-flex; align-items: center; gap: 8px; max-width: 140px; min-height: 28px; padding: 0 7px 0 8px; border: 0; border-radius: 6px; background: #e6f1fb; color: #1769aa; font: inherit; font-weight: 700; line-height: 1; cursor: pointer; transition: background-color .18s ease, color .18s ease, box-shadow .18s ease; }
.map-anchor-trigger:hover:not(:disabled) { background: #d9eaf8; color: #0f5a98; }
.map-anchor-trigger:focus-visible { outline: 2px solid #1769aa; outline-offset: 2px; }
.map-anchor-trigger:disabled { cursor: wait; opacity: .55; }
.map-anchor-chevron { width: 7px; height: 7px; flex: none; margin-top: -4px; border-right: 2px solid currentColor; border-bottom: 2px solid currentColor; transform: rotate(45deg); transition: transform .2s cubic-bezier(.2,.8,.2,1); }
.map-anchor-picker.is-open .map-anchor-chevron { margin-top: 4px; transform: rotate(225deg); }
.map-anchor-options { position: absolute; left: 8px; bottom: calc(100% + 9px); z-index: 20; display: grid; gap: 3px; min-width: 150px; padding: 5px; border: 1px solid #cfe0ee; border-radius: 10px; background: rgb(255 255 255 / .98); box-shadow: 0 14px 30px #17324d2b, 0 2px 6px #17324d14; pointer-events: auto; transform-origin: bottom left; }
.map-anchor-menu-enter-active, .map-anchor-menu-leave-active { transition: opacity .18s ease, transform .22s cubic-bezier(.2,.8,.2,1); }
.map-anchor-menu-enter-from, .map-anchor-menu-leave-to { opacity: 0; transform: translateY(8px) scale(.96); }
.map-anchor-option { display: grid; grid-template-columns: 28px 1fr 16px; align-items: center; gap: 6px; min-height: 32px; padding: 0 8px; border: 0; border-radius: 6px; background: transparent; color: #344455; font: inherit; font-size: var(--font-caption); text-align: left; cursor: pointer; transition: background-color .15s ease, color .15s ease, transform .15s ease; }
.map-anchor-option:hover, .map-anchor-option:focus-visible { background: #edf5fb; color: #1769aa; transform: translateX(2px); outline: none; }
.map-anchor-option.selected { background: #e6f1fb; color: #1769aa; font-weight: 700; }
.map-anchor-option-key { color: #1769aa; font-weight: 700; }
.map-anchor-check { color: #087b76; font-size: 15px; font-weight: 800; text-align: center; }
.multi-route-comparison-note { margin: 12px 0 0; color: var(--ui-muted); font-size: var(--font-caption); }
.cesium-map { visibility: hidden; pointer-events: none; }
.cesium-map.visible { visibility: visible; pointer-events: auto; }
.map-top { flex-wrap: wrap; z-index: 3; }
.flight-entry { display: grid; place-items: center; width: 38px; min-height: var(--map-control-height); margin-left: auto; padding: 0; border: 1px solid var(--map-control-border); border-radius: var(--map-control-radius); background: #fff; box-shadow: var(--map-control-shadow); color: #126cb7; cursor: pointer; pointer-events: auto; }
.flight-entry .svg-icon { width: 22px; height: 22px; }
.flight-entry:disabled { opacity: .45; cursor: default; }
.flight-entry:hover:not(:disabled) { background: #f2f8fd; border-color: #bcd7ee; }
.flight-controls { display: flex; align-items: center; gap: 6px; flex: 0 0 min(340px, 100%); min-height: var(--map-control-height); padding: 4px 7px; border: 1px solid var(--map-control-border); border-radius: var(--map-control-radius); background: #fff; box-shadow: var(--map-control-shadow); pointer-events: auto; }
.flight-exit { flex: none; min-height: var(--map-control-height); padding: 0 14px; border: 1px solid var(--map-control-border); border-radius: var(--map-control-radius); background: #fff; box-shadow: var(--map-control-shadow); color: #b42318; font-size: var(--font-caption); font-weight: 600; white-space: nowrap; cursor: pointer; pointer-events: auto; }
.flight-exit:hover { background: #fdf1f0; border-color: #f1b7b1; }
.flight-toggle { display: grid; flex: none; place-items: center; width: 33px; height: 33px; padding: 0; border: 0; border-radius: 7px; background: #e6f1fb; color: #126cb7; }
.flight-toggle[aria-pressed="true"] { background: #126cb7; color: #fff; }
.flight-toggle:disabled { opacity: .45; cursor: default; }
.flight-toggle .svg-icon { width: 22px; height: 22px; }
.icon-drone { -webkit-mask-image: url('/svg/drone.svg'); mask-image: url('/svg/drone.svg'); }
.flight-speed { display: inline-flex; align-items: center; gap: 3px; flex: none; color: #526273; font-size: 12px; white-space: nowrap; }
.flight-speed .custom-select { display: inline-block; width: 68px; }
.flight-speed :deep(.custom-select-trigger) { min-height: 30px; padding: 0 8px; border-color: #dbe2ea; border-radius: 6px; background-color: #f7fafc; color: #1769aa; font-size: 12px; font-weight: 700; }
.flight-speed :deep(.custom-select-trigger:disabled) { opacity: .5; background-color: #f7fafc; }
.flight-progress-wrap { display: flex; align-items: center; flex: 1; min-width: 60px; min-height: 30px; }
.flight-progress { width: 100%; margin: 0; accent-color: #126cb7; cursor: pointer; }
.flight-progress:disabled { opacity: .4; cursor: default; }
.flight-percent { flex: none; width: 34px; color: #526273; font-size: 12px; font-variant-numeric: tabular-nums; text-align: right; }
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
.rc2-import-heading { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 12px; }
.rc2-import-heading h3 { margin: 0; color: #173b5c; font-size: 14px; }
.rc2-import-heading button { padding: 5px 8px; border: 1px solid #a9c8e4; border-radius: 6px; background: #fff; color: #1265a7; font-size: 12px; cursor: pointer; }
.rc2-import-panel p, .rc2-import-panel small, .rc2-job span { display: block; margin: 0; color: #53677c; font-size: 12px; line-height: 1.5; overflow-wrap: anywhere; }
.rc2-import-panel .form-error { color: #a4262c; }
.rc2-import-panel label { display: block; color: #344455; font-size: 12px; font-weight: 600; }
.rc2-import-panel .custom-select { display: block; margin-top: 5px; }
.rc2-import-panel :deep(.custom-select-trigger) { border-color: #b8cddd; border-radius: 6px; color: #25394d; font-size: 12px; }
.rc2-import-panel button:disabled { cursor: default; opacity: .55; }
.rc2-job { display: grid; gap: 6px; padding: 10px; border: 1px solid #bdd7ed; border-radius: 7px; background: #fff; }
.rc2-job > strong { color: #1265a7; font-size: 12px; }
.rc2-job-done { border-color: #9bd2b2; background: #f3fcf6; }
.rc2-job-done > strong { color: #167348; }
.rc2-job-failed { border-color: #edbfc1; background: #fff8f8; }
.rc2-job-failed > strong { color: #a4262c; }
.rc2-job-steps { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 4px; margin: 2px 0; padding: 0; list-style: none; }
.rc2-job-steps li { padding: 5px 3px; border-radius: 5px; background: #edf2f7; color: #607286; font-size: 12px; text-align: center; }
.rc2-job-steps li.active { background: #dceeff; color: #0f6cbd; font-weight: 700; }
.rc2-job-steps li.done { background: #ddf3e5; color: #167348; }
.primary-action-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 6px; align-items: center; }
</style>

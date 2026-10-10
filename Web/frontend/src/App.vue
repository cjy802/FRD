<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import WeatherPanels from './WeatherPanels.vue'
import RouteWeatherPanel from './RouteWeatherPanel.vue'
import WeatherWindowsPanel from './WeatherWindowsPanel.vue'
import RoadWeatherComparison from './RoadWeatherComparison.vue'
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
// Datetime-local only keeps minutes; use the next minute so the initial value
// remains a future departure while the weather requests are being prepared.
const departureBeijing = ref(new Date(Date.now() + 60 * 1000 + 8 * 3600 * 1000).toISOString().slice(0, 16))
const loading = ref(false)
const checkingPoint = ref(false)
const error = ref('')
const pointHint = ref('')
const mapMode = ref<'start' | 'end'>('start')
const roadSource = ref<'local' | 'online'>('local')
const mapReady = ref(false)
const mapElement = ref<HTMLElement | null>(null)
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

function chooseDeparture(time: string) {
  departureBeijing.value = new Date(new Date(time).getTime() + 8 * 3600 * 1000).toISOString().slice(0, 16)
}

function chooseRoad(route: Preview) {
  cancelCalculation()
  preview.value = route
  previewKey.value = routeKey.value
  routeAssessment.value = null
  drawCesiumRoute()
}

async function calculate() {
  if (loading.value) return
  const controller = new AbortController()
  const requestVersion = ++routeRequestVersion
  const requestedKey = routeKey.value
  routeController = controller
  loading.value = true
  error.value = ''
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
    preview.value = result
    previewKey.value = requestedKey
    drawCesiumRoute()
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

    void calculate()
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
  if (!viewer || !Cesium) return
  viewer.entities.removeAll()
  const start = form.start
  const end = form.end
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
  viewer.entities.add({
    name: '道路参考路线 · 未验证飞行安全',
    polyline: {
      positions: Cesium.Cartesian3.fromDegreesArray(preview.value.geometry.coordinates.flatMap(([longitude, latitude]) => [longitude, latitude])),
      width: 8,
      material: Cesium.Color.fromCssColorString('#0f6cbd'),
      clampToGround: true,
    },
  })
  if (routeAssessment.value && weatherCoordinates.value) {
    const colors: Record<string, string> = { no_trigger: '#0f6cbd', attention: '#b66a00', elevated: '#b42318', incomplete: '#6e7781' }
    for (const segment of routeAssessment.value.weather_risk.segments) {
      viewer.entities.add({ name: `气象初筛第 ${segment.index + 1} 段 · ${segment.level}（非街道级预报）`,
        polyline: { positions: Cesium.Cartesian3.fromDegreesArray([...segment.start, ...segment.end]), width: 5,
          material: Cesium.Color.fromCssColorString(colors[segment.level] || '#6e7781'), clampToGround: true } })
    }
  }
  const centerLongitude = (start.longitude + end.longitude) / 2
  const centerLatitude = (start.latitude + end.latitude) / 2
  const cameraHeight = Math.max(620, preview.value.straight_distance_km * 2600)
  if (flyToRoute) {
    viewer.camera.flyTo({ destination: Cesium.Cartesian3.fromDegrees(centerLongitude, centerLatitude, cameraHeight), duration: 0.8 })
  }
}

function initMap() {
  Cesium = (window as any).Cesium
  if (!Cesium || !mapElement.value) return
  try {
    viewer = new Cesium.Viewer(mapElement.value, {
      baseLayer: false,
      terrainProvider: new Cesium.EllipsoidTerrainProvider(),
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
    viewer.resolutionScale = window.innerWidth <= 720 ? 1.25 : 1.5
    viewer.imageryLayers.addImageryProvider(new Cesium.OpenStreetMapImageryProvider({
      url: 'https://tile.openstreetmap.org/',
      credit: new Cesium.Credit('<a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">© OpenStreetMap contributors</a>', true),
    }))
    viewer.scene.globe.depthTestAgainstTerrain = false
    viewer.camera.setView({ destination: Cesium.Cartesian3.fromDegrees(119.95050, 31.67980, 900) })
    const clickHandler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas)
    clickHandler.setInputAction((event: any) => {
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

onMounted(() => {
  void calculate()
  if ((window as any).Cesium) initMap()
  else window.addEventListener('load', initMap, { once: true })
})
onBeforeUnmount(() => {
  stopColumnResize()
  cancelCalculation()
  window.removeEventListener('load', initMap)
  if (viewer && !viewer.isDestroyed()) viewer.destroy()
})
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <div class="brand"><strong>低空飞行决策工作台</strong><small>航线规划与天气评估</small></div>
      <div class="top-actions"><span class="prototype-tag">道路参考路线</span><span class="connection">原型阶段</span></div>
    </header>

    <nav class="workspace-tabs" aria-label="工作区面板">
      <button type="button" :aria-pressed="workspacePanel === 'settings'" aria-controls="mission-sidebar" @click="workspacePanel = 'settings'">航线设置</button>
      <button type="button" :aria-pressed="workspacePanel === 'map'" aria-controls="route-map" @click="workspacePanel = 'map'">航线地图</button>
      <button type="button" :aria-pressed="workspacePanel === 'overview'" aria-controls="mission-insights" @click="workspacePanel = 'overview'">任务概览</button>
    </nav>

    <main class="layout" :style="layoutStyle">
      <aside id="mission-sidebar" class="sidebar" :class="{ 'panel-active': workspacePanel === 'settings' }" aria-label="航线设置">
        <div class="section-heading"><span class="eyebrow">航线设置</span><h1>规划一次飞行</h1><p>默认点位位于常州信息职业技术大学。沿校园道路计算参考路线。</p></div>

        <form class="route-form" @submit.prevent="calculate" @invalid.capture="revealInvalidSetting">
          <nav class="settings-pages" aria-label="航线设置分组"><button type="button" :aria-pressed="settingsPage === 'points'" @click="settingsPage = 'points'">起点与终点</button><button type="button" :aria-pressed="settingsPage === 'mission'" @click="settingsPage = 'mission'">飞行参数</button></nav>
          <div class="settings-fields">
          <div class="settings-group" data-settings-page="points" :class="{ 'settings-active': settingsPage === 'points' }">
          <div class="point-heading"><span class="point-mark point-a">A</span><div><strong>起点</strong><small>{{ formatted(form.start) }}</small></div><button type="button" class="range-toggle" :class="{ active: showStartRange }" :aria-pressed="showStartRange" :disabled="loading || checkingPoint" title="显示或隐藏 35 米接路搜索范围" @click="toggleRange('start')">范围：{{ showStartRange ? '开' : '关' }}</button></div>
          <div class="coordinate-row"><label>纬度<input v-model.number="form.start.latitude" type="number" step="0.00001" min="-90" max="90" required :disabled="loading" /></label><label>经度<input v-model.number="form.start.longitude" type="number" step="0.00001" min="-180" max="180" required :disabled="loading" /></label></div>

          <div class="route-divider" aria-hidden="true"></div>

          <div class="point-heading"><span class="point-mark point-b">B</span><div><strong>终点</strong><small>{{ formatted(form.end) }}</small></div><button type="button" class="range-toggle" :class="{ active: showEndRange }" :aria-pressed="showEndRange" :disabled="loading || checkingPoint" title="显示或隐藏 35 米接路搜索范围" @click="toggleRange('end')">范围：{{ showEndRange ? '开' : '关' }}</button></div>
          <div class="coordinate-row"><label>纬度<input v-model.number="form.end.latitude" type="number" step="0.00001" min="-90" max="90" required :disabled="loading" /></label><label>经度<input v-model.number="form.end.longitude" type="number" step="0.00001" min="-180" max="180" required :disabled="loading" /></label></div>
          </div>

          <div class="settings-group" data-settings-page="mission" :class="{ 'settings-active': settingsPage === 'mission' }">
          <div class="form-line"></div>
          <div class="coordinate-row"><label>离地高度 AGL · 米<input v-model.number="form.altitude_m" type="number" min="0" max="1000" required :disabled="loading" /></label><label>巡航速度 · km/h<input v-model.number="form.speed_kmh" type="number" min="1" max="200" step="0.5" required :disabled="loading" /></label></div>
          <small class="form-scope-note">目前高度风覆盖 10–100 米；范围外会标记缺测。</small>
          <label class="departure-field">出发时刻 · 北京时间<input v-model="departureBeijing" type="datetime-local" required /><small>沿线 ETA 按填写速度估算，不含风对地速的影响。</small></label>
          <div class="clearance-row"><label>建筑缓冲 · 米（演示）<input v-model.number="form.clearance_m" type="number" min="0" max="20" step="0.5" required :disabled="loading || roadSource === 'online'" /></label><span>{{ roadSource === 'local' ? '用于排除靠近建筑的道路段' : '在线机动车路线不使用此参数' }}</span></div>
          </div>
          </div>
          <fluent-button class="primary-button" appearance="primary" type="submit" :disabled="loading">{{ loading ? '计算中…' : '计算道路路线' }}</fluent-button>
          <p v-if="error" class="form-error" role="alert">{{ error }}</p>
        </form>
      </aside>

      <section id="route-map" class="map-panel" :class="{ 'panel-active': workspacePanel === 'map' }" aria-label="航线地图">
        <div ref="mapElement" class="cesium-map" :class="{ visible: mapReady }"></div>
        <div v-if="!mapReady" class="map-fallback">
          <div class="grid-lines"></div>
          <p>地图底图加载中</p><small>道路路线按所选数据源计算</small>
        </div>
        <div class="map-top"><div class="map-title-group"><span class="map-badge"><i></i> 道路参考路线 · 未验证飞行安全</span><div class="map-source-switch" aria-label="路线数据源"><button type="button" :class="{ active: roadSource === 'local' }" :aria-pressed="roadSource === 'local'" :disabled="loading || checkingPoint" @click="chooseRoadSource('local')">本地</button><button type="button" :class="{ active: roadSource === 'online' }" :aria-pressed="roadSource === 'online'" :disabled="loading || checkingPoint" @click="chooseRoadSource('online')">在线</button></div></div><span class="map-caption">{{ checkingPoint ? '正在检查点位…' : `地图点击可设置 ${mapMode === 'start' ? '起点 A' : '终点 B'}` }}</span></div>
        <div v-if="error" class="map-alert" role="alert">{{ error }}</div>
        <div v-else-if="pointHint" class="map-point-hint" role="status">{{ pointHint }}</div>
        <div class="map-bottom"><div class="map-switch"><button :class="{ active: mapMode === 'start' }" :disabled="checkingPoint" @click="mapMode = 'start'">设置起点 A</button><button :class="{ active: mapMode === 'end' }" :disabled="checkingPoint" @click="mapMode = 'end'">设置终点 B</button></div><span class="map-radius">接路判定 {{ landingSearchRadiusM }} m</span><span class="map-scale">任务高度 {{ form.altitude_m }} m</span></div>
        <div v-if="routeAssessment" class="weather-map-legend">沿线初筛：蓝 · 未触发，橙 · 关注，红 · 较强，灰 · 缺测<br />基于 0.25° 场，不是街道级风险，也不代表飞行许可。</div>
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

      <aside id="mission-insights" class="insights" :class="{ 'panel-active': workspacePanel === 'overview' }" aria-label="任务概览" tabindex="0">
        <div class="insights-header"><span class="eyebrow">任务状态</span><h2>先看天气结论</h2><p>先看结论，需要细节时再展开。</p></div>
        <section class="overview-hero" aria-label="天气结论概览">
        <div class="mission-facts"><div><span>路线长度</span><strong>{{ distance }}</strong></div><div><span>预计用时</span><strong>{{ elapsed }}</strong></div><div><span>计划出发</span><strong>{{ departureBeijing.replace('T', ' ') }}</strong><small>北京时间</small></div></div>
        <div class="decision" :class="decisionTone" aria-live="polite"><span class="decision-kicker">气象初筛</span><strong>{{ decisionTitle }}</strong><p>{{ decisionDescription }}</p><small>这里只反映已覆盖的天气指标，不等于放飞许可。</small><details v-if="routeAssessment"><summary>查看判断依据</summary><div v-for="check in weatherChecks" :key="check.key" class="decision-check"><b>{{ check.label }}</b><span :class="check.state === 'ready' ? 'check-ready' : 'check-pending'">{{ check.state === 'ready' ? '资料已就绪' : check.state === 'no_trigger' ? '未触发当前阈值' : check.state === 'elevated' ? '较强提示' : check.state === 'attention' ? '需要留意' : '资料待补齐' }}</span><small>{{ check.detail }}</small></div></details></div>
        </section>
        <details class="overview-group" open>
          <summary><span>沿线天气检查</span><small>风、降雨、阵风和能见度</small></summary>
          <div class="overview-group-body overview-weather">
            <WeatherPanels :altitude-m="form.altitude_m" :start="form.start" :end="form.end" @job-ready="weatherJobId = $event" @state-changed="weatherState = $event" />
            <RouteWeatherPanel :job-id="weatherJobId" :coordinates="weatherCoordinates" :departure-beijing="departureBeijing" :speed-kmh="form.speed_kmh" :altitude-m="form.altitude_m" :limits="activeLimits" @assessed="routeAssessment = $event" @phase="routePhase = $event" />
          </div>
        </details>
        <details class="overview-group"><summary><span>调整出发时间或路线</span><small>可选参考，不会自动替换当前设置</small></summary><div class="overview-group-body overview-planning"><WeatherWindowsPanel :job-id="weatherJobId" :coordinates="weatherCoordinates" :departure-beijing="departureBeijing" :speed-kmh="form.speed_kmh" :altitude-m="form.altitude_m" :limits="activeLimits" @choose="chooseDeparture" @state="windowPhase = $event" /><RoadWeatherComparison :job-id="weatherJobId" :can-compare="!!weatherCoordinates" :current-coordinates="weatherCoordinates" :start="form.start" :end="form.end" :road-source="roadSource" :clearance-m="form.clearance_m" :departure-beijing="departureBeijing" :speed-kmh="form.speed_kmh" :altitude-m="form.altitude_m" :limits="activeLimits" @choose="chooseRoad" @state="comparisonPhase = $event" /></div></details>
        <details class="overview-group"><summary><span>天气规则和技术状态</span><small>仅在需要核对时查看</small></summary><div class="overview-group-body"><details class="limits-settings"><summary>天气初筛阈值</summary><label class="limits-toggle"><input v-model="customLimitsEnabled" type="checkbox" /> 填写自定义气象阈值</label><p>关闭时使用演示阈值。阈值用于天气提示与比较，参考分不是事故概率。</p><template v-if="customLimitsEnabled"><label>阈值名称<input v-model="customLimits.name" maxlength="80" /></label><div class="coordinate-row"><label>最大高度风 · m/s<input v-model.number="customLimits.max_wind_m_s" type="number" min="0.1" max="200" step="0.1" required /></label><label>最大侧风 · m/s<input v-model.number="customLimits.max_crosswind_m_s" type="number" min="0.1" max="200" step="0.1" required /></label></div><div class="coordinate-row"><label>最大逆风 · m/s<input v-model.number="customLimits.max_headwind_m_s" type="number" min="0.1" max="200" step="0.1" required /></label><label>最大地面阵风 · m/s<input v-model.number="customLimits.max_gust_m_s" type="number" min="0.1" max="200" step="0.1" required /></label></div><div class="coordinate-row"><label>最大雨量 · mm/6h<input v-model.number="customLimits.max_rain_6h_mm" type="number" min="0.1" max="500" step="0.1" required /></label><label>最低地面能见度 · m<input v-model.number="customLimits.min_visibility_m" type="number" min="1" max="100000" step="1" required /></label></div></template></details><div class="pipeline"><h3>系统状态</h3><div><span>01</span><p>{{ alignmentProgress }}</p></div><div><span>02</span><p>{{ routeAssessment ? decisionTitle : '沿线初筛等待资料' }}</p></div><div><span>03</span><p>{{ planningProgress }}</p></div></div></div></details>
        <div v-if="preview" class="road-summary"><strong>当前路线：{{ preview.road_names.join(' → ') || '未命名道路' }}</strong><span>{{ distance }} · 预计用时 {{ elapsed }}</span><details><summary>查看路线数据</summary><small>直线 {{ preview.straight_distance_km.toFixed(2) }} km · {{ preview.building_clearance_m === null ? '在线模式未检查建筑缓冲' : `建筑缓冲 ${preview.building_clearance_m} m` }}</small><small>A/B 到道路分别约 {{ preview.start_snap_m }} m / {{ preview.end_snap_m }} m</small><small>{{ preview.data_source }}</small><small>路线数据 © <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap contributors</a></small></details></div>
      </aside>
    </main>
  </div>
</template>

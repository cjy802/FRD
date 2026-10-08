<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import WeatherPanels from './WeatherPanels.vue'

type Point = { latitude: number; longitude: number }
type Preview = {
  geometry: { coordinates: number[][] }
  distance_km: number
  straight_distance_km: number
  estimated_minutes: number
  altitude_m: number
  road_names: string[]
  start_snap_m: number
  end_snap_m: number
  building_clearance_m: number | null
  data_source: string
  risk: { level: string; score: number | null; reason: string }
}
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
let viewer: any = null
let Cesium: any = null
let routeController: AbortController | null = null
let routeRequestVersion = 0

const elapsed = computed(() => preview.value ? `${preview.value.estimated_minutes} 分钟` : '待计算')
const distance = computed(() => preview.value ? `${preview.value.distance_km.toFixed(2)} km` : '待计算')

function formatted(point: Point) {
  return `${point.latitude.toFixed(4)}°N, ${point.longitude.toFixed(4)}°E`
}

async function calculate() {
  if (loading.value) return
  const controller = new AbortController()
  const requestVersion = ++routeRequestVersion
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

    <main class="layout">
      <aside class="sidebar">
        <div class="section-heading"><span class="eyebrow">航线设置</span><h1>规划一次飞行</h1><p>默认点位位于常州信息职业技术大学。沿校园道路计算参考路线。</p></div>

        <form class="route-form" @submit.prevent="calculate">
          <div class="point-heading"><span class="point-mark point-a">A</span><div><strong>起点</strong><small>{{ formatted(form.start) }}</small></div><button type="button" class="range-toggle" :class="{ active: showStartRange }" :aria-pressed="showStartRange" :disabled="loading || checkingPoint" title="显示或隐藏 35 米接路搜索范围" @click="toggleRange('start')">范围：{{ showStartRange ? '开' : '关' }}</button></div>
          <div class="coordinate-row"><label>纬度<input v-model.number="form.start.latitude" type="number" step="0.00001" min="-90" max="90" required :disabled="loading" /></label><label>经度<input v-model.number="form.start.longitude" type="number" step="0.00001" min="-180" max="180" required :disabled="loading" /></label></div>

          <div class="route-divider" aria-hidden="true"></div>

          <div class="point-heading"><span class="point-mark point-b">B</span><div><strong>终点</strong><small>{{ formatted(form.end) }}</small></div><button type="button" class="range-toggle" :class="{ active: showEndRange }" :aria-pressed="showEndRange" :disabled="loading || checkingPoint" title="显示或隐藏 35 米接路搜索范围" @click="toggleRange('end')">范围：{{ showEndRange ? '开' : '关' }}</button></div>
          <div class="coordinate-row"><label>纬度<input v-model.number="form.end.latitude" type="number" step="0.00001" min="-90" max="90" required :disabled="loading" /></label><label>经度<input v-model.number="form.end.longitude" type="number" step="0.00001" min="-180" max="180" required :disabled="loading" /></label></div>

          <div class="form-line"></div>
          <div class="coordinate-row"><label>飞行高度 · 米<input v-model.number="form.altitude_m" type="number" min="0" max="1000" required :disabled="loading" /></label><label>巡航速度 · km/h<input v-model.number="form.speed_kmh" type="number" min="1" max="200" step="1" required :disabled="loading" /></label></div>
          <div class="clearance-row"><label>建筑缓冲 · 米（演示）<input v-model.number="form.clearance_m" type="number" min="0" max="20" step="0.5" required :disabled="loading || roadSource === 'online'" /></label><span>{{ roadSource === 'local' ? '用于排除靠近建筑的道路段' : '在线机动车路线不使用此参数' }}</span></div>
          <fluent-button class="primary-button" appearance="primary" type="submit" :disabled="loading">{{ loading ? '计算中…' : '计算道路路线' }}</fluent-button>
          <p v-if="error" class="form-error" role="alert">{{ error }}</p>
        </form>
        <p class="sidebar-note">{{ roadSource === 'local' ? '本地模式读取本地 OSM 路网，并按已收录建筑轮廓排除相交路段。' : '在线模式使用 OSRM 机动车道路路线，不检查建筑缓冲。' }} 两种路线均未核实线缆、树木、空域及天气，不能直接用于飞行。</p>
      </aside>

      <section class="map-panel" aria-label="航线地图">
        <div ref="mapElement" class="cesium-map" :class="{ visible: mapReady }"></div>
        <div v-if="!mapReady" class="map-fallback">
          <div class="grid-lines"></div>
          <p>地图底图加载中</p><small>道路路线按所选数据源计算</small>
        </div>
        <div class="map-top"><div class="map-title-group"><span class="map-badge"><i></i> 道路参考路线 · 未验证飞行安全</span><div class="map-source-switch" aria-label="路线数据源"><button type="button" :class="{ active: roadSource === 'local' }" :aria-pressed="roadSource === 'local'" :disabled="loading || checkingPoint" @click="chooseRoadSource('local')">本地</button><button type="button" :class="{ active: roadSource === 'online' }" :aria-pressed="roadSource === 'online'" :disabled="loading || checkingPoint" @click="chooseRoadSource('online')">在线</button></div></div><span class="map-caption">{{ checkingPoint ? '正在检查点位…' : `地图点击可设置 ${mapMode === 'start' ? '起点 A' : '终点 B'}` }}</span></div>
        <div v-if="error" class="map-alert" role="alert">{{ error }}</div>
        <div v-else-if="pointHint" class="map-point-hint" role="status">{{ pointHint }}</div>
        <div class="map-bottom"><div class="map-switch"><button :class="{ active: mapMode === 'start' }" :disabled="checkingPoint" @click="mapMode = 'start'">设置起点 A</button><button :class="{ active: mapMode === 'end' }" :disabled="checkingPoint" @click="mapMode = 'end'">设置终点 B</button></div><span class="map-radius">接路判定 {{ landingSearchRadiusM }} m</span><span class="map-scale">任务高度 {{ form.altitude_m }} m</span></div>
        <div v-if="loading" class="map-loading-overlay">
          <div class="map-loading-card">
            <button type="button" class="map-loading-cancel" aria-label="取消路线计算" title="取消" @click="cancelCalculation">×</button>
            <span class="map-loading-spinner" aria-hidden="true"></span>
            <strong role="status" aria-live="polite">加载中…</strong>
            <span>{{ roadSource === 'online' ? '正在获取在线道路路线' : '正在计算本地道路路线' }}</span>
          </div>
        </div>
      </section>

      <aside class="insights">
        <div class="insights-header"><span class="eyebrow">任务状态</span><h2>任务概览</h2></div>
        <div class="metric-pair"><div><span>道路路线</span><strong>{{ distance }}</strong></div><div><span>理论航时</span><strong>{{ elapsed }}</strong></div></div>
        <div v-if="preview" class="road-summary"><strong>{{ preview.road_names.join(' → ') || '未命名道路' }}</strong><span>直线 {{ preview.straight_distance_km.toFixed(2) }} km · {{ preview.building_clearance_m === null ? '在线模式未检查建筑缓冲' : `建筑缓冲 ${preview.building_clearance_m} m` }}</span><small>A/B 到道路分别约 {{ preview.start_snap_m }} m / {{ preview.end_snap_m }} m</small><small>{{ preview.data_source }}</small><small>路线数据 © <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap contributors</a></small></div>
          <WeatherPanels :altitude-m="form.altitude_m" :start="form.start" :end="form.end" />
        <div class="pipeline"><h3>接下来接入</h3><div><span>01</span><p>气象网格与时间、高度对齐</p></div><div><span>02</span><p>沿线风险评分与预警</p></div><div><span>03</span><p>天气约束下的绕行与飞行窗口</p></div></div>
        <div class="decision">当前决策 <strong>数据不足，暂不可判定能否飞行</strong></div>
      </aside>
    </main>
  </div>
</template>

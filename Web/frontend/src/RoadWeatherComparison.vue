<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import type { RoadPreview } from './routeTypes'
import type { WeatherLimits, WeatherRisk } from './weatherTypes'

const props = defineProps<{ jobId: string | null; canCompare: boolean; currentCoordinates: number[][] | null
  start: { latitude: number; longitude: number }; end: { latitude: number; longitude: number }
  roadSource: 'local' | 'online'; clearanceM: number; departureBeijing: string; speedKmh: number; altitudeM: number; limits: WeatherLimits | null }>()
const emit = defineEmits<{ choose: [route: RoadPreview]; state: [phase: string] }>()
type Candidate = { route_id: string; label: string; route: RoadPreview
  weather: Pick<WeatherRisk, 'level' | 'label' | 'score_0_100' | 'summary' | 'coverage_complete'> }
type Comparison = { state: string; reason: string; recommended_route_id: string; candidates: Candidate[]; message: string }
const result = ref<Comparison | null>(null)
const pending = ref(false)
const error = ref('')
let controller: AbortController | null = null
let timer: ReturnType<typeof setTimeout> | null = null
function isCurrent(candidate: Candidate) { return JSON.stringify(candidate.route.geometry.coordinates) === JSON.stringify(props.currentCoordinates) }
async function compare() {
  if (!props.jobId || !props.canCompare) return
  const current = new AbortController(); controller = current; pending.value = true; emit('state', 'pending')
  try {
    const departure = new Date(`${props.departureBeijing}:00+08:00`)
    if (Number.isNaN(departure.getTime())) throw new Error('出发时刻无效。')
    const response = await fetch('/api/routes/compare-weather', { method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: current.signal,
      body: JSON.stringify({ weather_job_id: props.jobId, start: props.start, end: props.end, road_source: props.roadSource,
        clearance_m: props.clearanceM, departure_time_utc: departure.toISOString(), speed_kmh: props.speedKmh, altitude_m: props.altitudeM, profile: props.limits }) })
    const payload = await response.json()
    if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : '道路天气比较失败。')
    if (current.signal.aborted || current !== controller) return
    result.value = payload; emit('state', payload.state)
  } catch (cause) {
    if (!current.signal.aborted && current === controller) { error.value = cause instanceof Error ? cause.message : '路线比较失败。'; emit('state', 'failed') }
  } finally { if (current === controller) { pending.value = false; controller = null } }
}
watch(() => [props.jobId, props.canCompare, props.start, props.end, props.roadSource, props.clearanceM, props.departureBeijing, props.speedKmh, props.altitudeM, props.limits], () => {
  controller?.abort(); controller = null; if (timer) clearTimeout(timer)
  result.value = null; pending.value = false; error.value = ''; emit('state', 'waiting')
  if (props.jobId && props.canCompare) timer = setTimeout(() => { void compare() }, 450)
}, { immediate: true, deep: true })
onBeforeUnmount(() => { controller?.abort(); if (timer) clearTimeout(timer) })
</script>

<template>
  <div class="status-block" aria-label="天气约束路线比较">
    <div class="status-title"><span>道路天气比较</span><b class="pending">{{ pending ? '比较中' : result ? '候选已比较' : '待比较' }}</b></div>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-else-if="!result">{{ pending ? '正在生成真实道路备选并比较天气暴露…' : '等待当前道路路线与区域天气。' }}</p>
    <template v-else>
      <p>{{ result.reason }}</p>
      <ol class="weather-window-list"><li v-for="candidate in result.candidates" :key="candidate.route_id"><strong>{{ candidate.label }} · {{ candidate.route.distance_km.toFixed(2) }} km</strong><span>{{ candidate.route.road_names.join(' → ') || '未命名道路' }}</span><span>{{ candidate.weather.label }} · {{ candidate.weather.score_0_100 === null ? '缺测，不评分' : `参考分 ${candidate.weather.score_0_100.toFixed(1)}` }}</span><span>高度风 {{ candidate.weather.summary.max_wind_m_s?.toFixed(2) ?? '—' }} m/s · 地面能见度 {{ candidate.weather.summary.min_surface_visibility_m?.toFixed(0) ?? '—' }} m</span><span v-if="candidate.route_id === result.recommended_route_id">{{ result.state === 'candidate_lower_screening' ? '相对较低初筛分备选（不是飞行许可）' : '比较器保留道路基线' }}</span><button type="button" class="weather-refresh" :disabled="isCurrent(candidate) || !candidate.weather.coverage_complete" @click="emit('choose', candidate.route)">{{ isCurrent(candidate) ? '当前道路参考线' : '采用并重新初筛' }}</button></li></ol>
      <small>备选均来自所选路网；不生成穿越建筑的直线捷径。在线机动车路线仍不核验建筑缓冲或空中障碍。</small><small>{{ result.message }}</small>
    </template>
  </div>
</template>

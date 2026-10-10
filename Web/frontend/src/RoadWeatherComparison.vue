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
function comparisonSummary(state: string) {
  return ({
    candidate_lower_screening: '备选路线的天气提醒较少，仍需人工确认。',
    no_alternative: '没有找到合适的其他道路。',
    coverage_incomplete: '部分路线天气资料不完整。',
    no_resolvable_weather_difference: '现有路线的天气差异不足以支持绕行。',
    no_material_improvement: '备选路线没有明显改善。',
  } as Record<string, string>)[state] || '路线天气比较已完成，请按需查看备选。'
}
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
    <div class="status-title"><span>路线备选</span><b class="pending">{{ pending ? '比较中' : result ? '已完成比较' : '待比较' }}</b></div>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-else-if="!result">{{ pending ? '正在生成路线备选并比较天气…' : '如需比较其他道路，先完成当前路线和天气检查。' }}</p>
    <template v-else>
      <p class="plain-summary">{{ comparisonSummary(result.state) }}</p>
      <details class="weather-rule-details"><summary>查看路线备选（{{ result.candidates.length }} 条）</summary><p>{{ result.reason }}</p><ol class="weather-window-list"><li v-for="candidate in result.candidates" :key="candidate.route_id"><strong>{{ candidate.label }} · {{ candidate.route.distance_km.toFixed(2) }} km</strong><span>{{ candidate.weather.label }} · {{ candidate.weather.coverage_complete ? '天气资料完整' : '天气资料不完整' }}</span><span v-if="candidate.route_id === result.recommended_route_id">{{ result.state === 'candidate_lower_screening' ? '相对较低的天气初筛提醒' : '当前路线仍是基线' }}</span><details><summary>查看路线和详细数值</summary><small>{{ candidate.route.road_names.join(' → ') || '未命名道路' }}</small><small>飞行高度风 {{ candidate.weather.summary.max_wind_m_s?.toFixed(1) ?? '—' }} m/s · 地面能见度 {{ candidate.weather.summary.min_surface_visibility_m?.toFixed(0) ?? '—' }} m</small><small>内部参考分：{{ candidate.weather.score_0_100 === null ? '缺测，不评分' : candidate.weather.score_0_100.toFixed(1) }}（不是安全概率）</small></details><button type="button" class="weather-refresh" :disabled="isCurrent(candidate) || !candidate.weather.coverage_complete" @click="emit('choose', candidate.route)">{{ isCurrent(candidate) ? '当前道路参考线' : '采用并重新初筛' }}</button></li></ol><small>备选来自所选道路数据源，天气比较受 0.25° 网格和资料覆盖范围限制。</small><small>{{ result.message }}</small></details>
    </template>
  </div>
</template>

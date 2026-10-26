<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { RouteAssessment, WeatherLimits } from './weatherTypes'

const props = defineProps<{
  jobId: string | null; coordinates: number[][] | null
  departureBeijing: string; speedKmh: number; altitudeM: number
  limits?: WeatherLimits | null
  savedAssessment?: RouteAssessment | null
}>()
const emit = defineEmits<{
  assessed: [result: RouteAssessment | null]
  phase: [state: { pending: boolean; error: string }]
  'limits-change': [limits: WeatherLimits]
}>()
const DEFAULT_LIMITS: WeatherLimits = { name: '演示规则', max_wind_m_s: 12, max_crosswind_m_s: 8,
  max_headwind_m_s: 12, max_gust_m_s: 15, max_rain_6h_mm: 5, min_visibility_m: 3000 }
const alignment = ref<RouteAssessment | null>(null)
const pending = ref(false)
const error = ref('')
const ageClock = ref(Date.now())
const CACHE_KEY = 'low-altitude-route-weather-v1'
const CACHE_TTL_MS = 6 * 60 * 60 * 1000
const assessmentAgeMs = computed(() => alignment.value?.tested_at_utc
  ? Math.max(0, ageClock.value - new Date(alignment.value.tested_at_utc).getTime()) : 0)
const assessmentAgeLabel = computed(() => {
  const minutes = Math.floor(assessmentAgeMs.value / 60_000)
  if (minutes < 1) return '刚刚'
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.floor(minutes / 60)
  const remainingMinutes = minutes % 60
  return remainingMinutes ? `${hours} 小时 ${remainingMinutes} 分钟前` : `${hours} 小时前`
})
const displayedLimits = computed(() => props.limits ?? alignment.value?.weather_risk.limits ?? DEFAULT_LIMITS)
let controller: AbortController | null = null
let timer: ReturnType<typeof setTimeout> | null = null
let expiryTimer: ReturnType<typeof setInterval> | null = null

function metricTone(field: string) {
  const level = alignment.value?.weather_risk.alerts.find(alert => alert.field === field)?.level
  return level === 'elevated' ? 'is-danger' : level === 'attention' ? 'is-attention' : 'is-ok'
}
function updateLimit(field: 'max_wind_m_s' | 'max_crosswind_m_s' | 'min_visibility_m', event: Event) {
  const value = Number((event.target as HTMLInputElement).value)
  if (!Number.isFinite(value) || value <= 0) return
  emit('limits-change', { ...displayedLimits.value, name: props.limits?.name ?? '自定义阈值', [field]: value })
}
function assessmentCacheKey() {
  if (!props.jobId || !props.coordinates) return ''
  const signature = JSON.stringify({
    job_id: props.jobId, coordinates: props.coordinates,
    speed_kmh: props.speedKmh, altitude_m: props.altitudeM, profile: props.limits ?? null,
  })
  let hash = 2166136261
  for (let index = 0; index < signature.length; index += 1) {
    hash = Math.imul(hash ^ signature.charCodeAt(index), 16777619)
  }
  return `route-weather-v1-${(hash >>> 0).toString(16)}`
}
function freshAssessment(value: RouteAssessment | null | undefined, key: string): RouteAssessment | null {
  if (!value || value.assessment_cache_key !== key || !value.tested_at_utc) return null
  const testedAt = new Date(value.tested_at_utc).getTime()
  const age = Date.now() - testedAt
  return age >= 0 && age < CACHE_TTL_MS ? { ...value, cache_reused: true } : null
}
function restoreAssessment(key: string): RouteAssessment | null {
  try {
    const stored = JSON.parse(localStorage.getItem(CACHE_KEY) ?? 'null') as RouteAssessment | null
    const cached = freshAssessment(stored, key)
    if (cached) return cached
  } catch {
    // Ignore a missing, expired, or unreadable browser cache.
  }
  return freshAssessment(props.savedAssessment, key)
}
function saveAssessment(value: RouteAssessment) {
  try { localStorage.setItem(CACHE_KEY, JSON.stringify(value)) } catch {
    // The active mission record still stores the assessment when a task exists.
  }
}
async function requestAlignment() {
  if (!props.jobId || !props.coordinates) return
  const current = new AbortController()
  controller = current
  error.value = ''
  pending.value = true
  emit('phase', { pending: true, error: '' })
  try {
    const departure = new Date(`${props.departureBeijing}:00+08:00`)
    if (Number.isNaN(departure.getTime())) throw new Error('请填写有效的北京时间出发时刻。')
    const response = await fetch(`/api/weather/predictions/${encodeURIComponent(props.jobId)}/route`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: current.signal,
      body: JSON.stringify({ coordinates: props.coordinates, departure_time_utc: departure.toISOString(), speed_kmh: props.speedKmh, altitude_m: props.altitudeM, profile: props.limits ?? null }),
    })
    const payload = await response.json()
    if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : '航线天气时间匹配失败，请检查任务参数。')
    if (current.signal.aborted || current !== controller) return
    const assessed = { ...payload, tested_at_utc: new Date().toISOString(),
      assessment_cache_key: assessmentCacheKey(), cache_reused: false } as RouteAssessment
    alignment.value = assessed
    saveAssessment(assessed)
    emit('assessed', assessed)
  } catch (cause) {
    if (!current.signal.aborted && current === controller) error.value = cause instanceof Error ? cause.message : '航线天气时间匹配失败。'
  } finally {
    if (current === controller) { pending.value = false; controller = null; emit('phase', { pending: false, error: error.value }) }
  }
}
watch(() => [props.jobId, props.coordinates, props.speedKmh, props.altitudeM, props.limits], () => {
  controller?.abort()
  controller = null
  if (timer) clearTimeout(timer)
  pending.value = false
  error.value = ''
  emit('phase', { pending: false, error: '' })
  const key = assessmentCacheKey()
  const cached = key ? restoreAssessment(key) : null
  alignment.value = cached
  emit('assessed', cached)
  if (cached) {
    saveAssessment(cached)
    return
  }
  if (props.jobId && props.coordinates) timer = setTimeout(() => { void requestAlignment() }, 250)
}, { immediate: true, deep: true })
onMounted(() => {
  expiryTimer = setInterval(() => {
    ageClock.value = Date.now()
    if (alignment.value && assessmentAgeMs.value >= CACHE_TTL_MS) {
      controller?.abort(); controller = null; alignment.value = null; pending.value = false
      error.value = '沿线天气测试结果已超过六小时，请刷新天气后重新测试。'
      emit('assessed', null); emit('phase', { pending: false, error: error.value })
    }
  }, 30000)
})
onBeforeUnmount(() => { controller?.abort(); if (timer) clearTimeout(timer); if (expiryTimer) clearInterval(expiryTimer) })
</script>

<template>
  <div class="status-block" aria-label="沿线天气检查">
    <div class="status-title"><span>沿线天气检查</span></div>
    <template v-if="alignment">
      <div class="weather-test-meta"><small>上次测试：{{ assessmentAgeLabel }}</small><button type="button" :disabled="pending" @click="requestAlignment">{{ pending ? '正在测试…' : '重新测试' }}</button></div>
      <div class="weather-mini-grid"><div :class="metricTone('wind')"><span>飞行高度风</span><strong>{{ alignment.weather_risk.summary.max_wind_m_s?.toFixed(1) ?? '—' }} m/s</strong><small>最高风速</small></div><div :class="metricTone('crosswind')"><span>侧风</span><strong>{{ alignment.weather_risk.summary.max_crosswind_m_s?.toFixed(1) ?? '—' }} m/s</strong><small>横向风力</small></div><div :class="metricTone('visibility')"><span>地面能见度</span><strong>{{ alignment.weather_risk.summary.min_surface_visibility_m?.toFixed(0) ?? '—' }} m</strong><small>最低值</small></div></div>
    </template>
    <p v-else>{{ pending ? '正在计算沿线高度风及风雨提示…' : error || '等待路线与天气资料就绪。' }}</p>
    <details v-if="coordinates" class="weather-threshold-config route-threshold-config" aria-label="沿线天气阈值配置">
      <summary class="weather-threshold-config-heading"><strong>设置沿线天气阈值</strong></summary>
      <div class="weather-threshold-group">
        <div class="weather-threshold-item"><strong>飞行高度风 · m/s</strong><label>较强提示线<input type="number" min="0.1" max="200" step="0.1" :value="displayedLimits.max_wind_m_s" @change="updateLimit('max_wind_m_s', $event)" /></label><small>关注线：{{ (displayedLimits.max_wind_m_s * .8).toFixed(1) }} m/s</small></div>
        <div class="weather-threshold-item"><strong>侧风 · m/s</strong><label>较强提示线<input type="number" min="0.1" max="200" step="0.1" :value="displayedLimits.max_crosswind_m_s" @change="updateLimit('max_crosswind_m_s', $event)" /></label><small>关注线：{{ (displayedLimits.max_crosswind_m_s * .8).toFixed(1) }} m/s</small></div>
        <div class="weather-threshold-item"><strong>地面能见度 · m</strong><label>较强提示线<input type="number" min="1" max="100000" step="1" :value="displayedLimits.min_visibility_m" @change="updateLimit('min_visibility_m', $event)" /></label><small>关注线：≤ {{ (displayedLimits.min_visibility_m / .8).toFixed(0) }} m</small></div>
      </div>
      <small class="weather-threshold-note">高度风覆盖离地 10–100 米；地面能见度和阵风是地面指标。预报网格与时次有限，不代表放飞许可。</small>
    </details>
    <div v-if="!alignment && !pending && jobId && coordinates" class="weather-test-meta"><small>暂无六小时内的测试结果</small><button type="button" @click="requestAlignment">{{ error ? '重新测试' : '开始测试' }}</button></div>
  </div>
</template>

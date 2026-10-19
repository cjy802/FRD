<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { RouteAssessment, WeatherLimits } from './weatherTypes'

const props = defineProps<{
  jobId: string | null; coordinates: number[][] | null
  departureBeijing: string; speedKmh: number; altitudeM: number
  limits?: WeatherLimits | null
}>()
const emit = defineEmits<{
  assessed: [result: RouteAssessment | null]
  phase: [state: { pending: boolean; error: string }]
  'point-weather-toggle': [visible: boolean]
}>()
const alignment = ref<RouteAssessment | null>(null)
const pending = ref(false)
const error = ref('')
const pointWeatherVisible = ref(false)
let controller: AbortController | null = null
let timer: ReturnType<typeof setTimeout> | null = null
let expiryTimer: ReturnType<typeof setInterval> | null = null

function beijing(time: string) {
  return new Date(time).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
}
function riskLabel(level: string, fallback: string) {
  return ({ incomplete: '资料不完整', elevated: '需要重点留意', attention: '需要留意', no_trigger: '暂未发现明显提醒' } as Record<string, string>)[level] || fallback
}
function riskDescription(level: string, complete: boolean) {
  if (!complete || level === 'incomplete') return '部分路线缺少可用天气资料，当前结果不能作为完整判断。'
  if (level === 'no_trigger') return '已覆盖的风、降雨、阵风和能见度没有超过当前提醒线。'
  if (level === 'elevated') return '有指标达到较强提醒线，已在上方红框标出；可打开地图逐点复核。'
  return '有指标接近提醒线，已在上方橙框标出；可打开地图逐点复核。'
}
function metricTone(field: string) {
  const level = alignment.value?.weather_risk.alerts.find(alert => alert.field === field)?.level
  return level === 'elevated' ? 'is-danger' : level === 'attention' ? 'is-attention' : ''
}
function togglePointWeather() {
  pointWeatherVisible.value = !pointWeatherVisible.value
  emit('point-weather-toggle', pointWeatherVisible.value)
}
async function requestAlignment() {
  if (!props.jobId || !props.coordinates) return
  const current = new AbortController()
  controller = current
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
    alignment.value = payload
    emit('assessed', payload)
  } catch (cause) {
    if (!current.signal.aborted && current === controller) error.value = cause instanceof Error ? cause.message : '航线天气时间匹配失败。'
  } finally {
    if (current === controller) { pending.value = false; controller = null; emit('phase', { pending: false, error: error.value }) }
  }
}
watch(() => [props.jobId, props.coordinates], () => {
  pointWeatherVisible.value = false
  emit('point-weather-toggle', false)
}, { deep: true, flush: 'sync' })
watch(() => [props.jobId, props.coordinates, props.departureBeijing, props.speedKmh, props.altitudeM, props.limits], () => {
  controller?.abort()
  controller = null
  if (timer) clearTimeout(timer)
  alignment.value = null
  emit('assessed', null)
  pending.value = false
  error.value = ''
  emit('phase', { pending: false, error: '' })
  if (props.jobId && props.coordinates) timer = setTimeout(() => { void requestAlignment() }, 250)
}, { immediate: true, deep: true })
onMounted(() => {
  expiryTimer = setInterval(() => {
    if (alignment.value && new Date(alignment.value.arrival_time_utc).getTime() < Date.now()) {
      controller?.abort(); controller = null; alignment.value = null; pending.value = false
      error.value = '预计到达时刻已过去，请返回天气页重新匹配当前时刻。'
      emit('assessed', null); emit('phase', { pending: false, error: error.value })
    }
  }, 30000)
})
onBeforeUnmount(() => { controller?.abort(); if (timer) clearTimeout(timer); if (expiryTimer) clearInterval(expiryTimer) })
</script>

<template>
  <div class="status-block" aria-label="沿线天气检查">
    <div class="status-title"><span>沿线天气检查</span><b :class="alignment ? `risk-${alignment.weather_risk.level}` : 'pending'">{{ alignment ? riskLabel(alignment.weather_risk.level, alignment.weather_risk.label) : '待评估' }}</b></div>
    <template v-if="alignment">
      <p class="plain-summary">{{ riskDescription(alignment.weather_risk.level, alignment.weather_risk.coverage_complete) }}</p>
      <div class="weather-mini-grid"><div :class="metricTone('wind')"><span>飞行高度风</span><strong>{{ alignment.weather_risk.summary.max_wind_m_s?.toFixed(1) ?? '—' }} m/s</strong><small>最高风速</small></div><div :class="metricTone('crosswind')"><span>侧风</span><strong>{{ alignment.weather_risk.summary.max_crosswind_m_s?.toFixed(1) ?? '—' }} m/s</strong><small>横向风力</small></div><div :class="metricTone('visibility')"><span>地面能见度</span><strong>{{ alignment.weather_risk.summary.min_surface_visibility_m?.toFixed(0) ?? '—' }} m</strong><small>最低值</small></div></div>
      <details class="weather-rule-details"><summary>查看规则和数据范围</summary><small>当前使用{{ alignment.weather_risk.rule_mode === 'demo' ? '演示' : '自定义' }}提醒线；飞行高度风 {{ alignment.weather_risk.limits.max_wind_m_s }}、侧风 {{ alignment.weather_risk.limits.max_crosswind_m_s }}、逆风 {{ alignment.weather_risk.limits.max_headwind_m_s }} m/s；地面阵风 {{ alignment.weather_risk.limits.max_gust_m_s }} m/s；6 小时降雨 {{ alignment.weather_risk.limits.max_rain_6h_mm }} mm；能见度最低 {{ alignment.weather_risk.limits.min_visibility_m }} m。</small><small>参考分仅用于内部排序，不是事故概率。高度风覆盖离地 10–100 米；阵风和能见度是地面指标。</small><small>0.25° 网格与 6 小时时次，不能代表街道级预报。</small></details>
    </template>
    <p v-else>{{ pending ? '正在计算沿线高度风及风雨提示…' : error || '等待路线与天气资料就绪。' }}</p>
  </div>
  <div class="status-block route-weather-panel" aria-label="任务时间和高度">
    <div class="status-title"><span>任务时间和高度</span><b :class="alignment?.coverage.flight_height ? 'risk-no_trigger' : 'pending'">{{ pending ? '匹配中' : alignment?.coverage.flight_height ? '资料完整' : alignment?.coverage.surface_space_time ? '缺少高度资料' : '待匹配' }}</b></div>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-else-if="!alignment">{{ pending ? '正在匹配沿线经过时刻的天气…' : !coordinates ? '请先计算与当前起终点一致的道路路线。' : '等待区域天气预测完成。' }}</p>
    <template v-else>
      <p>出发 {{ beijing(alignment.departure_time_utc) }} → 到达 {{ beijing(alignment.arrival_time_utc) }}（北京时间）</p>
      <small>{{ alignment.samples.length }}/{{ alignment.sample_count }} 个路线点有天气资料；预计飞行 {{ (alignment.duration_seconds / 60).toFixed(1) }} 分钟。</small>
      <p v-if="alignment.missing.length || alignment.height_missing.length" class="weather-inline-warning" role="alert">部分路段缺少预测，结果不完整。</p>
      <p v-else class="plain-summary">整条路线的时间和飞行高度都有对应天气资料。</p>
      <button v-if="alignment.samples.length" type="button" class="weather-point-toggle" :aria-pressed="pointWeatherVisible" @click="togglePointWeather">{{ pointWeatherVisible ? '隐藏地图点位天气' : '查看沿线每个点的天气' }}</button>
    </template>
  </div>
</template>

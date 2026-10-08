<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { RouteAssessment, WeatherLimits } from './weatherTypes'

const props = defineProps<{
  jobId: string | null; coordinates: number[][] | null
  departureBeijing: string; speedKmh: number; altitudeM: number
  limits?: WeatherLimits | null
}>()
const emit = defineEmits<{ assessed: [result: RouteAssessment | null]; phase: [state: { pending: boolean; error: string }] }>()
const alignment = ref<RouteAssessment | null>(null)
const pending = ref(false)
const error = ref('')
let controller: AbortController | null = null
let timer: ReturnType<typeof setTimeout> | null = null
let expiryTimer: ReturnType<typeof setInterval> | null = null

function beijing(time: string) {
  return new Date(time).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
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
    if (alignment.value && new Date(alignment.value.departure_time_utc).getTime() < Date.now()) {
      controller?.abort(); controller = null; alignment.value = null; pending.value = false
      error.value = '出发时刻已过去，请调整任务时刻后重新初筛。'
      emit('assessed', null); emit('phase', { pending: false, error: error.value })
    }
  }, 30000)
})
onBeforeUnmount(() => { controller?.abort(); if (timer) clearTimeout(timer); if (expiryTimer) clearInterval(expiryTimer) })
</script>

<template>
  <div class="status-block" aria-label="气象风险">
    <div class="status-title"><span>气象风险 · 沿线初筛</span><b :class="alignment ? `risk-${alignment.weather_risk.level}` : 'pending'">{{ alignment?.weather_risk.label || '待评估' }}</b></div>
    <template v-if="alignment">
      <p>高度风最高 {{ alignment.weather_risk.summary.max_wind_m_s?.toFixed(2) ?? '—' }} m/s · 侧风最高 {{ alignment.weather_risk.summary.max_crosswind_m_s?.toFixed(2) ?? '—' }} m/s</p>
      <small>地面阵风最高 {{ alignment.weather_risk.summary.max_surface_gust_m_s?.toFixed(2) ?? '—' }} m/s · 地面能见度最低 {{ alignment.weather_risk.summary.min_surface_visibility_m?.toFixed(0) ?? '—' }} m</small>
      <small>参考分 {{ alignment.weather_risk.score_0_100 === null ? '缺测，不评分' : `${alignment.weather_risk.score_0_100.toFixed(1)} / 100` }}；{{ alignment.weather_risk.rule_mode === 'demo' ? '演示规则，不是机型飞行限值' : '自定义阈值，仍需核验机型手册' }}。</small>
      <ul v-if="alignment.weather_risk.alerts.length" class="weather-triggers"><li v-for="alert in alignment.weather_risk.alerts" :key="alert.field">第 {{ alert.point_index + 1 }} 个点 · {{ beijing(alert.eta_utc) }}<br />{{ alert.coordinate[1]?.toFixed(5) }}°N / {{ alert.coordinate[0]?.toFixed(5) }}°E：{{ alert.reason }}</li></ul>
      <p v-else>{{ alignment.weather_risk.coverage_complete ? '所覆盖的风雨及地面能见度未触发当前规则。' : '天气未完整覆盖，不能以未触发提示作为初筛通过。' }}</p>
      <details class="weather-rule-details"><summary>规则与未覆盖因素</summary><p>{{ alignment.weather_risk.score_definition }}</p><small>风 {{ alignment.weather_risk.limits.max_wind_m_s }}、侧风 {{ alignment.weather_risk.limits.max_crosswind_m_s }}、逆风 {{ alignment.weather_risk.limits.max_headwind_m_s }}、地面阵风 {{ alignment.weather_risk.limits.max_gust_m_s }} m/s；雨量 {{ alignment.weather_risk.limits.max_rain_6h_mm }} mm/6h；地面能见度 {{ alignment.weather_risk.limits.min_visibility_m }} m。最明显触发位置列在上方。</small><small v-for="limitation in alignment.weather_risk.limitations" :key="limitation">{{ limitation }}</small></details>
    </template>
    <p v-else>{{ pending ? '正在计算沿线高度风及风雨提示…' : error || '等待路线与天气资料就绪。' }}</p>
  </div>
  <div class="status-block route-weather-panel" aria-label="沿线时间匹配">
    <div class="status-title"><span>沿线时空与高度</span><b :class="alignment?.coverage.flight_height ? 'risk-no_trigger' : 'pending'">{{ pending ? '匹配中' : alignment?.coverage.flight_height ? '高度风已匹配' : alignment?.coverage.surface_space_time ? '缺高度资料' : '待匹配' }}</b></div>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-else-if="!alignment">{{ pending ? '正在匹配沿线经过时刻的天气…' : !coordinates ? '请先计算与当前起终点一致的道路路线。' : '等待区域天气预测完成。' }}</p>
    <template v-else>
      <p>出发 {{ beijing(alignment.departure_time_utc) }} → 到达 {{ beijing(alignment.arrival_time_utc) }}（北京时间）</p>
      <small>{{ alignment.samples.length }}/{{ alignment.sample_count }} 个沿线点有地面天气；理论航时 {{ (alignment.duration_seconds / 60).toFixed(2) }} 分钟。</small>
      <p v-if="alignment.missing.length" role="alert">覆盖不足：{{ alignment.missing[0]?.reason }}（共 {{ alignment.missing.length }} 个点）</p>
      <p v-if="alignment.height_missing.length" role="alert">高度覆盖不足：{{ alignment.height_missing[0]?.reason }}</p>
      <details v-if="alignment.samples.length" class="weather-rule-details">
        <summary>查看沿线经过时刻与地面天气</summary>
        <div class="route-weather-samples">
          <p v-for="sample in alignment.samples" :key="sample.index">{{ sample.index + 1 }} · {{ beijing(sample.eta_utc) }} · {{ sample.coordinate[1]?.toFixed(5) }}°N / {{ sample.coordinate[0]?.toFixed(5) }}°E<br />温度 {{ (sample.weather.values.t2m_K - 273.15).toFixed(1) }}°C · 10 米风 {{ sample.weather.derived.wind_speed_10m_m_s.toFixed(2) }} m/s<br /><template v-if="sample.height_weather">{{ altitudeM }} 米 AGL 风 {{ sample.height_weather.wind_speed_m_s.toFixed(2) }} m/s（{{ sample.height_weather.height_method === 'native_height' ? '原生高度层' : '高度插值估计' }}）；地面阵风 {{ sample.height_weather.gust_surface_m_s.toFixed(2) }} m/s；地面能见度 {{ (sample.height_weather.visibility_surface_m / 1000).toFixed(1) }} km<br /></template>{{ sample.weather.time_method === 'native_time' ? '原生时次' : '6 小时时次间估计' }}；雨量 {{ sample.weather.values.tp_6h_mm.toFixed(2) }} mm，区间 {{ beijing(sample.weather.rain_interval.start_utc) }}–{{ beijing(sample.weather.rain_interval.end_utc) }}</p>
        </div>
      </details>
      <small>{{ alignment.message }}</small>
      <small>{{ alignment.eta_warning }}</small>
      <small>0.25° 原生网格；累计降水不是分钟雨强。高度风来自 GFS，不套用地面订正；地面阵风/能见度不代表空中湍流或云中能见度。</small>
    </template>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'

const props = defineProps<{
  jobId: string | null; coordinates: number[][] | null
  departureBeijing: string; speedKmh: number; altitudeM: number
}>()
type Weather = {
  values: { t2m_K: number; tp_6h_mm: number }
  derived: { wind_speed_10m_m_s: number }
  time_method: string; rain_interval: { start_utc: string; end_utc: string }
}
type Alignment = {
  state: string; departure_time_utc: string; arrival_time_utc: string
  sample_count: number; duration_seconds: number; message: string
  samples: { index: number; coordinate: number[]; eta_utc: string; weather: Weather; height_weather: { wind_speed_m_s: number; height_method: string; gust_surface_m_s: number; visibility_surface_m: number } | null }[]
  missing: { index: number; reason: string }[]
  height_missing: { index: number; reason: string }[]
  coverage: { surface_space_time: boolean; flight_height: boolean }
}
const alignment = ref<Alignment | null>(null)
const pending = ref(false)
const error = ref('')
let controller: AbortController | null = null
let timer: ReturnType<typeof setTimeout> | null = null

function beijing(time: string) {
  return new Date(time).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
}
async function requestAlignment() {
  if (!props.jobId || !props.coordinates) return
  const current = new AbortController()
  controller = current
  pending.value = true
  try {
    const departure = new Date(`${props.departureBeijing}:00+08:00`)
    if (Number.isNaN(departure.getTime())) throw new Error('请填写有效的北京时间出发时刻。')
    const response = await fetch(`/api/weather/predictions/${encodeURIComponent(props.jobId)}/route`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: current.signal,
      body: JSON.stringify({ coordinates: props.coordinates, departure_time_utc: departure.toISOString(), speed_kmh: props.speedKmh, altitude_m: props.altitudeM }),
    })
    const payload = await response.json()
    if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : '航线天气时间匹配失败，请检查任务参数。')
    if (current.signal.aborted || current !== controller) return
    alignment.value = payload
  } catch (cause) {
    if (!current.signal.aborted && current === controller) error.value = cause instanceof Error ? cause.message : '航线天气时间匹配失败。'
  } finally {
    if (current === controller) { pending.value = false; controller = null }
  }
}
watch(() => [props.jobId, props.coordinates, props.departureBeijing, props.speedKmh, props.altitudeM], () => {
  controller?.abort()
  controller = null
  if (timer) clearTimeout(timer)
  alignment.value = null
  pending.value = false
  error.value = ''
  if (props.jobId && props.coordinates) timer = setTimeout(() => { void requestAlignment() }, 250)
}, { immediate: true, deep: true })
onBeforeUnmount(() => { controller?.abort(); if (timer) clearTimeout(timer) })
</script>

<template>
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
      <small>0.25° 原生网格；累计降水不是分钟雨强。高度风来自 GFS，不套用地面订正；地面阵风/能见度不代表空中湍流或云中能见度。</small>
    </template>
  </div>
</template>

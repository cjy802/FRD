<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = defineProps<{ altitudeM: number; start: { latitude: number; longitude: number }; end: { latitude: number; longitude: number } }>()
const emit = defineEmits<{ jobReady: [jobId: string | null]; stateChanged: [state: { status: string; message: string }] }>()
type Field = 't2m_K' | 'u10_m_s' | 'v10_m_s' | 'msl_Pa' | 'tp_6h_mm'
type Forecast = {
  init_time_utc: string; valid_time_utc: string; generated_at_utc: string
  lead_hours: number; historical: boolean
  input_source?: string
  native_resolution_deg?: number; model_warning?: string
  initialization_frames?: { valid_time_utc: string; state_cycle_utc: string; state_forecast_hours: number }[]
  point: { latitude_deg: number; longitude_deg: number }
  values: Record<Field, number>
  derived: { wind_speed_10m_m_s: number; wind_direction_from_deg: number | null }
  point_risk: {
    level: string; label: string; message: string; route_reason: string
    triggers: { field: string; label: string; value: number; unit: string; threshold: number }[]
    rules: { wind_attention_m_s: number; wind_elevated_m_s: number; rain_attention_6h_mm: number; rain_elevated_6h_mm: number }
  }
}
type GroundSeries = {
  samples: { offset_hours: number; interval_label: string; valid_time_utc: string; time_method: string
    values: { t2m_K: number; u10_m_s: number; v10_m_s: number; msl_Pa: number; tp_6h_mm: number }
    derived: { wind_speed_10m_m_s: number; wind_direction_from_deg: number | null } }[]
  rain: { total_mm_6h: number; interval_start_utc: string; interval_end_utc: string; is_hourly_allocation: boolean; note: string }
}
type Job = {
  job_id: string; status: 'queued' | 'running' | 'succeeded' | 'blocked' | 'failed'
  message: string; forecast: Forecast | null; cache_hit?: boolean; cache_age_seconds?: number
}
type WeatherCache = { job_id: string; forecast: Forecast; ground_series: GroundSeries | null }
const WEATHER_CACHE_KEY = 'low-altitude-latest-weather-v1'
const MAX_CACHE_AGE_MS = 6 * 60 * 60 * 1000

const forecast = ref<Forecast | null>(null)
const loading = ref(false)
const message = ref('进入页面后自动生成未来天气预报。')
const error = ref('')
const jobStatus = ref<Job['status'] | null>(null)
const completedJob = ref('')
const displayedForecastJob = ref('')
const sampling = ref(false)
const groundSeries = ref<GroundSeries | null>(null)
const seriesLoading = ref(false)
const ageClock = ref(Date.now())
const cacheHit = ref(false)
const staleForecastKey = ref('')
let activeController: AbortController | null = null
let sampleController: AbortController | null = null
let pollTimer: ReturnType<typeof setTimeout> | null = null
let expiryTimer: ReturnType<typeof setInterval> | null = null
let pollStartedAt = 0
const pollTimeoutMs = 30 * 60 * 1000
const metrics: { field: Field; label: string }[] = [
  { field: 't2m_K', label: '温度 · °C' },
  { field: 'u10_m_s', label: 'U 风 · m/s' },
  { field: 'v10_m_s', label: 'V 风 · m/s' },
  { field: 'msl_Pa', label: '气压 · Pa' },
  { field: 'tp_6h_mm', label: '6 小时降水 · mm' },
]
const weatherLabel = computed(() => loading.value ? '预测中' : sampling.value ? '位置匹配中' : forecast.value ? '未来约 6 小时' : '等待天气预报')
const forecastAgeMs = computed(() => forecast.value ? Math.max(0, ageClock.value - new Date(forecast.value.generated_at_utc).getTime()) : 0)
const forecastAgeLabel = computed(() => {
  const minutes = Math.floor(forecastAgeMs.value / 60_000)
  if (minutes < 1) return '刚刚'
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.floor(minutes / 60)
  const remainingMinutes = minutes % 60
  return remainingMinutes ? `${hours} 小时 ${remainingMinutes} 分钟前` : `${hours} 小时前`
})
const forecastNeedsRefresh = computed(() => Boolean(forecast.value
  && (forecastAgeMs.value >= MAX_CACHE_AGE_MS || new Date(forecast.value.valid_time_utc).getTime() <= ageClock.value)))
function riskLabel(level: string, fallback: string) {
  return ({ elevated: '需要重点留意', attention: '需要留意', no_trigger: '暂未发现明显提醒', incomplete: '资料不完整' } as Record<string, string>)[level] || fallback
}
const groundSummary = computed(() => {
  if (!forecast.value) return '等待未来天气预报生成后评估路线中点附近天气。'
  return forecast.value.point_risk.triggers.length ? '路线中点附近有风雨因素需要留意。' : '路线中点附近暂未发现超过当前提醒线的风雨。'
})
const routePoint = computed(() => ({
  latitude: (props.start.latitude + props.end.latitude) / 2,
  longitude: (props.start.longitude + props.end.longitude) / 2,
}))

function saveWeatherCache() {
  if (!forecast.value || !displayedForecastJob.value) return
  try {
    const value: WeatherCache = { job_id: displayedForecastJob.value, forecast: forecast.value, ground_series: groundSeries.value }
    localStorage.setItem(WEATHER_CACHE_KEY, JSON.stringify(value))
  } catch {
    // The backend cache remains available if browser storage is unavailable.
  }
}

function restoreWeatherCache(): boolean {
  try {
    const raw = localStorage.getItem(WEATHER_CACHE_KEY)
    if (!raw) return false
    const value = JSON.parse(raw) as WeatherCache
    if (!value.job_id || !value.forecast?.generated_at_utc || !value.forecast?.valid_time_utc) return false
    forecast.value = value.forecast
    displayedForecastJob.value = value.job_id
    groundSeries.value = value.ground_series ?? null
    message.value = '已恢复上一次天气结果，正在检查本机缓存。'
    jobStatus.value = 'succeeded'
    staleForecastKey.value = value.forecast.generated_at_utc
    return true
  } catch {
    return false
  }
}

async function sampleRouteWeather() {
  if (!completedJob.value || loading.value) return
  sampleController?.abort()
  const controller = new AbortController()
  sampleController = controller
  sampling.value = true
  seriesLoading.value = false
  try {
    const query = new URLSearchParams({ latitude: String(routePoint.value.latitude), longitude: String(routePoint.value.longitude) })
    const response = await fetch(`/api/weather/predictions/${encodeURIComponent(completedJob.value)}/sample?${query}`, {
      signal: controller.signal, cache: 'no-store',
    })
    const payload = await response.json()
    if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : '区域天气查询失败。')
    if (controller.signal.aborted || controller !== sampleController) return
    forecast.value = payload
    displayedForecastJob.value = completedJob.value
    groundSeries.value = null
    error.value = ''
    saveWeatherCache()
    void loadGroundSeries(controller)
  } catch (cause) {
    if (!controller.signal.aborted && controller === sampleController) error.value = cause instanceof Error ? cause.message : '区域天气查询失败。'
  } finally {
    if (controller === sampleController) { sampling.value = false; sampleController = null }
  }
}

async function loadGroundSeries(parentController: AbortController) {
  if (!completedJob.value || parentController.signal.aborted) return
  seriesLoading.value = true
  try {
    const query = new URLSearchParams({ latitude: String(routePoint.value.latitude), longitude: String(routePoint.value.longitude), hours: '6' })
    const response = await fetch(`/api/weather/predictions/${encodeURIComponent(completedJob.value)}/series?${query}`, {
      signal: parentController.signal, cache: 'no-store',
    })
    const payload = await response.json()
    if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : '未来地面趋势查询失败。')
    if (!parentController.signal.aborted) {
      groundSeries.value = payload
      saveWeatherCache()
    }
  } catch (cause) {
    if (!parentController.signal.aborted) error.value = cause instanceof Error ? cause.message : '未来地面趋势查询失败。'
  } finally {
    if (!parentController.signal.aborted) seriesLoading.value = false
  }
}

function localTime(time: string) {
  return new Date(time).toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false,
  })
}
function metricValue(field: Field, value: number) {
  return (field === 't2m_K' ? value - 273.15 : value).toFixed(2)
}
async function readJob(response: Response): Promise<Job> {
  const payload = await response.json()
  if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : '天气预测接口暂时不可用。')
  if (!payload.job_id || !['queued', 'running', 'succeeded', 'blocked', 'failed'].includes(payload.status)) {
    throw new Error('天气预测接口尚未更新，请重启后端。')
  }
  return payload
}
function finishError(controller: AbortController, cause: unknown) {
  if (controller.signal.aborted || controller !== activeController) return
  error.value = cause instanceof Error ? cause.message : '天气预测失败。'
  emit('stateChanged', { status: 'failed', message: error.value })
  loading.value = false
  activeController = null
}
function applyJob(job: Job, controller: AbortController) {
  if (controller.signal.aborted || controller !== activeController) return
  const changed = jobStatus.value !== job.status || message.value !== job.message
  jobStatus.value = job.status
  message.value = job.message
  cacheHit.value = job.cache_hit ?? false
  if (changed) emit('stateChanged', { status: job.status, message: job.message })
  if (job.status === 'succeeded') {
    if (!job.forecast || job.forecast.historical || new Date(job.forecast.valid_time_utc).getTime() <= Date.now()) {
      throw new Error('预报时刻已过期或接口返回历史数据，需要最新起报场。')
    }
    loading.value = false
    activeController = null
    if (!forecast.value || new Date(job.forecast.generated_at_utc).getTime()
      > new Date(forecast.value.generated_at_utc).getTime()) {
      forecast.value = job.forecast
      displayedForecastJob.value = job.job_id
      groundSeries.value = null
    }
    completedJob.value = job.job_id
    emit('jobReady', job.job_id)
    saveWeatherCache()
    void sampleRouteWeather()
    return
  }
  if (job.status === 'blocked' || job.status === 'failed') {
    loading.value = false
    activeController = null
    return
  }
  const elapsed = Date.now() - pollStartedAt
  if (elapsed >= pollTimeoutMs) throw new Error('天气推理等待超时，请稍后重新预测。')
  const delay = elapsed < 10_000 ? 2000 : elapsed < 60_000 ? 5000 : 10_000
  pollTimer = setTimeout(() => {
    pollTimer = null
    void pollJob(job.job_id, controller)
  }, Math.min(delay, pollTimeoutMs - elapsed))
}
async function pollJob(jobId: string, controller: AbortController) {
  if (controller.signal.aborted || controller !== activeController) return
  try {
    if (Date.now() - pollStartedAt >= pollTimeoutMs) throw new Error('天气推理等待超时，请稍后重新预测。')
    const job = await readJob(await fetch(`/api/weather/predictions/${encodeURIComponent(jobId)}`, {
      signal: controller.signal, cache: 'no-store',
    }))
    applyJob(job, controller)
  } catch (cause) { finishError(controller, cause) }
}
async function predictWeather(forceRefresh = false) {
  activeController?.abort()
  sampleController?.abort()
  sampleController = null
  sampling.value = false
  seriesLoading.value = false
  if (!forecast.value) {
    completedJob.value = ''
    groundSeries.value = null
    emit('jobReady', null)
  }
  if (pollTimer) clearTimeout(pollTimer)
  pollTimer = null
  const controller = new AbortController()
  activeController = controller
  pollStartedAt = Date.now()
  loading.value = true
  error.value = ''
  jobStatus.value = 'queued'
  message.value = forceRefresh ? '正在刷新天气预测…' : '正在检查六小时天气缓存…'
  emit('stateChanged', { status: 'queued', message: message.value })
  try {
    const responseUrl = forceRefresh ? '/api/weather/predict?refresh=true' : '/api/weather/predict'
    const job = await readJob(await fetch(responseUrl, {
      method: 'POST', signal: controller.signal, cache: 'no-store',
    }))
    applyJob(job, controller)
  } catch (cause) { finishError(controller, cause) }
}

onMounted(() => {
  restoreWeatherCache()
  void predictWeather()
  expiryTimer = setInterval(() => {
    ageClock.value = Date.now()
    if (forecastNeedsRefresh.value && forecast.value && !loading.value
      && staleForecastKey.value !== forecast.value.generated_at_utc) {
      staleForecastKey.value = forecast.value.generated_at_utc
      message.value = '天气缓存已超过六小时或有效时刻已过，正在自动刷新。'
      void predictWeather()
    }
  }, 30000)
})
watch(routePoint, () => { void sampleRouteWeather() })
onBeforeUnmount(() => {
  saveWeatherCache()
  activeController?.abort()
  sampleController?.abort()
  if (pollTimer) clearTimeout(pollTimer)
  if (expiryTimer) clearInterval(expiryTimer)
})
</script>

<template>
  <div class="status-block weather-risk-panel" aria-label="路线中点参考天气">
    <div class="status-title"><span>路线中点参考天气</span><b :class="forecast ? `risk-${forecast.point_risk.level}` : 'pending'">{{ forecast ? riskLabel(forecast.point_risk.level, forecast.point_risk.label) : (loading ? '等待预测' : '待评估') }}</b></div>
    <template v-if="forecast">
      <p class="plain-summary">{{ groundSummary }}</p>
      <div class="weather-mini-grid"><div><span>温度</span><strong>{{ (forecast.values.t2m_K - 273.15).toFixed(1) }}°C</strong></div><div><span>地面风</span><strong>{{ forecast.derived.wind_speed_10m_m_s.toFixed(1) }} m/s</strong></div><div><span>6 小时降雨</span><strong>{{ forecast.values.tp_6h_mm.toFixed(1) }} mm</strong></div></div>
      <details v-if="forecast.point_risk.triggers.length" class="weather-rule-details"><summary>查看需要留意的原因</summary><ul class="weather-triggers"><li v-for="trigger in forecast.point_risk.triggers" :key="trigger.field">{{ trigger.label }}：{{ trigger.value.toFixed(2) }} {{ trigger.unit }}（提醒线 {{ trigger.threshold }}）</li></ul></details>
      <small class="weather-scope">这里看的是起终点之间一个参考位置的地面天气；整条路线和任务高度请看“沿线天气检查”。</small>
      <details class="weather-rule-details"><summary>查看提醒规则</summary><p>风速达到 {{ forecast.point_risk.rules.wind_attention_m_s }} / {{ forecast.point_risk.rules.wind_elevated_m_s }} m/s，或 6 小时降雨达到 {{ forecast.point_risk.rules.rain_attention_6h_mm }} / {{ forecast.point_risk.rules.rain_elevated_6h_mm }} mm，会分别显示关注 / 较强提醒。</p><small>{{ forecast.point_risk.message }}</small></details>
    </template>
    <p v-else>等待未来天气预报生成后评估风雨提示。</p>
  </div>

  <div class="status-block weather-data-panel" aria-label="详细天气">
    <div class="status-title"><span>详细天气</span><b class="weather-state">{{ weatherLabel }}</b></div>
    <div class="weather-live-actions weather-cache-toolbar">
      <small v-if="forecast">上次预测：{{ localTime(forecast.generated_at_utc) }} · {{ forecastAgeLabel }}{{ cacheHit ? ' · 已复用缓存' : '' }}</small>
      <small v-else>首次预测会自动运行；之后优先复用六小时内的数据。</small>
      <button type="button" class="weather-refresh" :disabled="loading" @click="predictWeather(true)">{{ loading ? '刷新中…' : '刷新天气' }}</button>
    </div>
    <p v-if="forecast && loading" class="weather-refreshing" role="status">正在获取新预测，旧数据会暂时保留。</p>
    <p v-if="forecast && error" class="weather-cache-error" role="alert">{{ error }}；当前仍显示上一次结果。</p>
    <p v-if="!forecast" :role="jobStatus === 'failed' || error ? 'alert' : 'status'">{{ error || (sampling ? '正在查询航线位置的区域天气…' : message) }}</p>
    <template v-if="forecast">
      <p v-if="sampling" class="weather-refreshing" role="status">正在根据新的航线中点重新匹配天气，当前结果暂时保留。</p>
      <p class="plain-summary">有效时刻：{{ localTime(forecast.valid_time_utc) }}（北京时间）。需要核对时，再展开下面的详细数值和变化趋势。</p>
      <details class="weather-rule-details"><summary>查看完整天气数值</summary><table class="weather-values"><caption>模型输出</caption><thead><tr><th scope="col">变量</th><th scope="col">预报值</th></tr></thead><tbody><tr v-for="metric in metrics" :key="metric.field"><th scope="row">{{ metric.label }}</th><td>{{ metricValue(metric.field, forecast.values[metric.field]) }}</td></tr></tbody></table><div class="weather-derived"><span>10 米风速 <strong>{{ forecast.derived.wind_speed_10m_m_s.toFixed(2) }} m/s</strong></span><span>风向（来向）<strong>{{ forecast.derived.wind_direction_from_deg === null ? '静风' : `${forecast.derived.wind_direction_from_deg.toFixed(0)}°` }}</strong></span></div></details>
      <details class="weather-rule-details"><summary>查看未来 6 小时变化</summary><div class="ground-trend" aria-label="未来六小时地面趋势"><div class="ground-trend-heading"><strong>未来 6 小时地面趋势</strong><small>{{ seriesLoading ? '趋势查询中…' : '按小时查看' }}</small></div><table v-if="groundSeries" class="weather-values ground-trend-table"><caption>中间时刻为 6 小时时次之间的估计</caption><thead><tr><th scope="col">区间</th><th scope="col">温度</th><th scope="col">10 米风</th><th scope="col">气压</th></tr></thead><tbody><tr v-for="sample in groundSeries.samples" :key="sample.valid_time_utc"><th scope="row">{{ sample.interval_label }}</th><td>{{ (sample.values.t2m_K - 273.15).toFixed(1) }}°C</td><td>{{ sample.derived.wind_speed_10m_m_s.toFixed(1) }} m/s</td><td>{{ (sample.values.msl_Pa / 100).toFixed(0) }} hPa</td></tr></tbody></table><p v-if="groundSeries" class="ground-rain-note">降水：{{ groundSeries.rain.interval_start_utc ? `${localTime(groundSeries.rain.interval_start_utc)}–${localTime(groundSeries.rain.interval_end_utc)}累计 ${groundSeries.rain.total_mm_6h.toFixed(2)} mm` : '暂无' }}。{{ groundSeries.rain.note }}</p><p v-else class="ground-trend-empty">{{ seriesLoading ? '正在加载未来 6 小时趋势…' : '未来趋势暂不可用。' }}</p></div></details>
      <details class="weather-rule-details"><summary>查看数据来源和限制</summary><small>起终点中点 {{ forecast.point.latitude_deg.toFixed(5) }}°N，{{ forecast.point.longitude_deg.toFixed(5) }}°E（不代表整条航线）</small><small v-if="forecast.native_resolution_deg">原生网格 {{ forecast.native_resolution_deg }}°，空间插值不是街道级预报。</small><small v-if="forecast.model_warning">{{ forecast.model_warning }}</small><small v-if="forecast.input_source?.startsWith('NOAA GFS')">起报资料：NOAA 公开 GFS 气象场</small><small v-if="forecast.initialization_frames?.length" v-for="frame in forecast.initialization_frames" :key="frame.valid_time_utc">有效时刻 {{ localTime(frame.valid_time_utc) }}；GFS 周期 {{ localTime(frame.state_cycle_utc) }} +{{ frame.state_forecast_hours }}h（{{ frame.state_forecast_hours === 0 ? '分析场' : '同有效时刻短时背景场' }}）。</small><small>起报：{{ localTime(forecast.init_time_utc) }} · 提前量 {{ forecast.lead_hours }} 小时</small><small>生成：{{ localTime(forecast.generated_at_utc) }}（北京时间）</small></details>
    </template>
    <small class="weather-cache-note">缓存超过六小时或预报有效时刻已过时会自动刷新；也可随时点击“刷新天气”。降雨按六小时累计量显示。</small>
  </div>
</template>

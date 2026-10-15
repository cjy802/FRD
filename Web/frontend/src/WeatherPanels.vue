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
  message: string; forecast: Forecast | null
}

const forecast = ref<Forecast | null>(null)
const loading = ref(false)
const message = ref('进入页面后自动生成未来天气预报。')
const error = ref('')
const jobStatus = ref<Job['status'] | null>(null)
const completedJob = ref('')
const sampling = ref(false)
const groundSeries = ref<GroundSeries | null>(null)
const seriesLoading = ref(false)
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

async function sampleRouteWeather() {
  if (!completedJob.value || loading.value) return
  sampleController?.abort()
  const controller = new AbortController()
  sampleController = controller
  sampling.value = true
  groundSeries.value = null
  seriesLoading.value = false
  error.value = ''
  try {
    const query = new URLSearchParams({ latitude: String(routePoint.value.latitude), longitude: String(routePoint.value.longitude) })
    const response = await fetch(`/api/weather/predictions/${encodeURIComponent(completedJob.value)}/sample?${query}`, {
      signal: controller.signal, cache: 'no-store',
    })
    const payload = await response.json()
    if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : '区域天气查询失败。')
    if (controller.signal.aborted || controller !== sampleController) return
    forecast.value = payload
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
    if (!parentController.signal.aborted) groundSeries.value = payload
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
  if (changed) emit('stateChanged', { status: job.status, message: job.message })
  if (job.status === 'succeeded') {
    if (!job.forecast || job.forecast.historical || new Date(job.forecast.valid_time_utc).getTime() <= Date.now()) {
      throw new Error('预报时刻已过期或接口返回历史数据，需要最新起报场。')
    }
    loading.value = false
    activeController = null
    completedJob.value = job.job_id
    emit('jobReady', job.job_id)
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
async function predictWeather() {
  activeController?.abort()
  sampleController?.abort()
  sampleController = null
  sampling.value = false
  completedJob.value = ''
  groundSeries.value = null
  seriesLoading.value = false
  emit('jobReady', null)
  if (pollTimer) clearTimeout(pollTimer)
  pollTimer = null
  const controller = new AbortController()
  activeController = controller
  pollStartedAt = Date.now()
  loading.value = true
  error.value = ''
  forecast.value = null
  jobStatus.value = 'queued'
  message.value = '正在发起未来天气预测…'
  emit('stateChanged', { status: 'queued', message: message.value })
  try {
    const job = await readJob(await fetch('/api/weather/predict', {
      method: 'POST', signal: controller.signal, cache: 'no-store',
    }))
    applyJob(job, controller)
  } catch (cause) { finishError(controller, cause) }
}

onMounted(() => {
  void predictWeather()
  expiryTimer = setInterval(() => {
    if (forecast.value && new Date(forecast.value.valid_time_utc).getTime() <= Date.now()) {
      forecast.value = null; completedJob.value = ''; emit('jobReady', null)
      jobStatus.value = 'blocked'; message.value = '本次预报已过期，请点击重新预测以获取最新时次。'
      emit('stateChanged', { status: 'blocked', message: message.value })
    }
  }, 30000)
})
watch(routePoint, () => { void sampleRouteWeather() })
onBeforeUnmount(() => {
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
    <p v-if="!forecast" :role="jobStatus === 'failed' || error ? 'alert' : 'status'">{{ error || (sampling ? '正在查询航线位置的区域天气…' : message) }}</p>
    <template v-if="forecast">
      <p v-if="sampling" class="weather-refreshing" role="status">正在根据新的航线中点重新匹配天气，当前结果暂时保留。</p>
      <p class="plain-summary">有效时刻：{{ localTime(forecast.valid_time_utc) }}（北京时间）。需要核对时，再展开下面的详细数值和变化趋势。</p>
      <details class="weather-rule-details"><summary>查看完整天气数值</summary><table class="weather-values"><caption>模型输出</caption><thead><tr><th scope="col">变量</th><th scope="col">预报值</th></tr></thead><tbody><tr v-for="metric in metrics" :key="metric.field"><th scope="row">{{ metric.label }}</th><td>{{ metricValue(metric.field, forecast.values[metric.field]) }}</td></tr></tbody></table><div class="weather-derived"><span>10 米风速 <strong>{{ forecast.derived.wind_speed_10m_m_s.toFixed(2) }} m/s</strong></span><span>风向（来向）<strong>{{ forecast.derived.wind_direction_from_deg === null ? '静风' : `${forecast.derived.wind_direction_from_deg.toFixed(0)}°` }}</strong></span></div></details>
      <details class="weather-rule-details"><summary>查看未来 6 小时变化</summary><div class="ground-trend" aria-label="未来六小时地面趋势"><div class="ground-trend-heading"><strong>未来 6 小时地面趋势</strong><small>{{ seriesLoading ? '趋势查询中…' : '按小时查看' }}</small></div><table v-if="groundSeries" class="weather-values ground-trend-table"><caption>中间时刻为 6 小时时次之间的估计</caption><thead><tr><th scope="col">区间</th><th scope="col">温度</th><th scope="col">10 米风</th><th scope="col">气压</th></tr></thead><tbody><tr v-for="sample in groundSeries.samples" :key="sample.valid_time_utc"><th scope="row">{{ sample.interval_label }}</th><td>{{ (sample.values.t2m_K - 273.15).toFixed(1) }}°C</td><td>{{ sample.derived.wind_speed_10m_m_s.toFixed(1) }} m/s</td><td>{{ (sample.values.msl_Pa / 100).toFixed(0) }} hPa</td></tr></tbody></table><p v-if="groundSeries" class="ground-rain-note">降水：{{ groundSeries.rain.interval_start_utc ? `${localTime(groundSeries.rain.interval_start_utc)}–${localTime(groundSeries.rain.interval_end_utc)}累计 ${groundSeries.rain.total_mm_6h.toFixed(2)} mm` : '暂无' }}。{{ groundSeries.rain.note }}</p><p v-else class="ground-trend-empty">{{ seriesLoading ? '正在加载未来 6 小时趋势…' : '未来趋势暂不可用。' }}</p></div></details>
      <details class="weather-rule-details"><summary>查看数据来源和限制</summary><small>起终点中点 {{ forecast.point.latitude_deg.toFixed(5) }}°N，{{ forecast.point.longitude_deg.toFixed(5) }}°E（不代表整条航线）</small><small v-if="forecast.native_resolution_deg">原生网格 {{ forecast.native_resolution_deg }}°，空间插值不是街道级预报。</small><small v-if="forecast.model_warning">{{ forecast.model_warning }}</small><small v-if="forecast.input_source?.startsWith('NOAA GFS')">起报资料：NOAA 公开 GFS 气象场</small><small v-if="forecast.initialization_frames?.length" v-for="frame in forecast.initialization_frames" :key="frame.valid_time_utc">有效时刻 {{ localTime(frame.valid_time_utc) }}；GFS 周期 {{ localTime(frame.state_cycle_utc) }} +{{ frame.state_forecast_hours }}h（{{ frame.state_forecast_hours === 0 ? '分析场' : '同有效时刻短时背景场' }}）。</small><small>起报：{{ localTime(forecast.init_time_utc) }} · 提前量 {{ forecast.lead_hours }} 小时</small><small>生成：{{ localTime(forecast.generated_at_utc) }}（北京时间）</small></details>
    </template>
    <div class="weather-live-actions"><button type="button" class="weather-refresh" :disabled="loading" @click="predictWeather">{{ loading ? '预测中…' : '重新预测' }}</button><small>每次进入自动预测；预测结果会按所选航线中点更新。</small></div>
  </div>
</template>

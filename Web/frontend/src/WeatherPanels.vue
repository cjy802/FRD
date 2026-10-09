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
let activeController: AbortController | null = null
let sampleController: AbortController | null = null
let pollTimer: ReturnType<typeof setTimeout> | null = null
let expiryTimer: ReturnType<typeof setInterval> | null = null
const metrics: { field: Field; label: string }[] = [
  { field: 't2m_K', label: '温度 · °C' },
  { field: 'u10_m_s', label: 'U 风 · m/s' },
  { field: 'v10_m_s', label: 'V 风 · m/s' },
  { field: 'msl_Pa', label: '气压 · Pa' },
  { field: 'tp_6h_mm', label: '6 小时降水 · mm' },
]
const weatherLabel = computed(() => loading.value ? '预测中' : sampling.value ? '位置匹配中' : forecast.value ? '+6h 预报' : '等待最新起报场')
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
  } catch (cause) {
    if (!controller.signal.aborted && controller === sampleController) error.value = cause instanceof Error ? cause.message : '区域天气查询失败。'
  } finally {
    if (controller === sampleController) { sampling.value = false; sampleController = null }
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
function applyJob(job: Job, controller: AbortController, attempts: number) {
  if (controller.signal.aborted || controller !== activeController) return
  jobStatus.value = job.status
  message.value = job.message
  emit('stateChanged', { status: job.status, message: job.message })
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
  if (attempts >= 1800) throw new Error('天气推理等待超时，请稍后重新预测。')
  pollTimer = setTimeout(() => { void pollJob(job.job_id, controller, attempts + 1) }, 1000)
}
async function pollJob(jobId: string, controller: AbortController, attempts: number) {
  try {
    const job = await readJob(await fetch(`/api/weather/predictions/${encodeURIComponent(jobId)}`, {
      signal: controller.signal, cache: 'no-store',
    }))
    applyJob(job, controller, attempts)
  } catch (cause) { finishError(controller, cause) }
}
async function predictWeather() {
  activeController?.abort()
  sampleController?.abort()
  sampleController = null
  sampling.value = false
  completedJob.value = ''
  emit('jobReady', null)
  if (pollTimer) clearTimeout(pollTimer)
  const controller = new AbortController()
  activeController = controller
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
    applyJob(job, controller, 0)
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
  <div class="status-block weather-risk-panel" aria-label="地面风雨提示">
    <div class="status-title"><span>地面风雨提示</span><b :class="forecast ? `risk-${forecast.point_risk.level}` : 'pending'">{{ forecast?.point_risk.label || (loading ? '等待预测' : '待评估') }}</b></div>
    <template v-if="forecast">
      <p v-if="forecast.point_risk.triggers.length === 0">预报地面风雨未触发演示阈值。</p>
      <ul v-else class="weather-triggers"><li v-for="trigger in forecast.point_risk.triggers" :key="trigger.field">{{ trigger.label }}：{{ trigger.value.toFixed(2) }} {{ trigger.unit }}（≥ {{ trigger.threshold }}）</li></ul>
      <p class="weather-scope">这里仅是起终点中点的地面风雨提示；任务时刻、高度风和方向分量见沿线初筛。</p>
      <small>任务高度 {{ altitudeM }} m；当前风预报为地面以上 10 m。</small>
      <details class="weather-rule-details"><summary>查看单点提示规则</summary><p>风速 ≥ {{ forecast.point_risk.rules.wind_attention_m_s }} / {{ forecast.point_risk.rules.wind_elevated_m_s }} m/s，6 小时降水 ≥ {{ forecast.point_risk.rules.rain_attention_6h_mm }} / {{ forecast.point_risk.rules.rain_elevated_6h_mm }} mm，分别触发关注 / 较强提示。</p><small>{{ forecast.point_risk.message }}</small></details>
    </template>
    <p v-else>等待未来天气预报生成后评估风雨提示。</p>
  </div>

  <div class="status-block weather-data-panel" aria-label="天气数据">
    <div class="status-title"><span>天气数据</span><b class="weather-state">{{ weatherLabel }}</b></div>
    <p v-if="!forecast" :role="jobStatus === 'failed' || error ? 'alert' : 'status'">{{ error || (sampling ? '正在查询航线位置的区域天气…' : message) }}</p>
    <template v-if="forecast">
      <p v-if="sampling" class="weather-refreshing" role="status">正在根据新的航线中点重新匹配天气，当前结果暂时保留。</p>
      <p class="weather-forecast-time">有效时刻：{{ localTime(forecast.valid_time_utc) }}（北京时间）</p>
      <table class="weather-values"><caption>未来天气预报</caption><thead><tr><th scope="col">变量</th><th scope="col">预报值</th></tr></thead><tbody><tr v-for="metric in metrics" :key="metric.field"><th scope="row">{{ metric.label }}</th><td>{{ metricValue(metric.field, forecast.values[metric.field]) }}</td></tr></tbody></table>
      <div class="weather-derived"><span>10 米风速 <strong>{{ forecast.derived.wind_speed_10m_m_s.toFixed(2) }} m/s</strong></span><span>风向（来向）<strong>{{ forecast.derived.wind_direction_from_deg === null ? '静风' : `${forecast.derived.wind_direction_from_deg.toFixed(0)}°` }}</strong></span></div>
      <small>起终点中点 {{ forecast.point.latitude_deg.toFixed(5) }}°N，{{ forecast.point.longitude_deg.toFixed(5) }}°E（不代表整条航线）</small>
      <small v-if="forecast.native_resolution_deg">原生网格 {{ forecast.native_resolution_deg }}°，空间插值不是街道级预报。</small>
      <small v-if="forecast.model_warning">{{ forecast.model_warning }}</small>
      <small v-if="forecast.input_source?.startsWith('NOAA GFS')">起报资料：NOAA 公开 GFS 气象场</small>
      <details v-if="forecast.initialization_frames?.length" class="weather-rule-details"><summary>查看起报资料实际来源周期</summary><small v-for="frame in forecast.initialization_frames" :key="frame.valid_time_utc">有效时刻 {{ localTime(frame.valid_time_utc) }}；GFS 周期 {{ localTime(frame.state_cycle_utc) }} +{{ frame.state_forecast_hours }}h（{{ frame.state_forecast_hours === 0 ? '分析场' : '同有效时刻短时背景场' }}）。</small></details>
      <small>起报：{{ localTime(forecast.init_time_utc) }} · 提前量 {{ forecast.lead_hours }} 小时</small>
      <small>生成：{{ localTime(forecast.generated_at_utc) }}（北京时间）</small>
    </template>
    <div class="weather-live-actions"><button type="button" class="weather-refresh" :disabled="loading" @click="predictWeather">{{ loading ? '预测中…' : '重新预测' }}</button><small>每次进入自动预测；+6h 从最新起报场时刻计算。</small></div>
  </div>
</template>

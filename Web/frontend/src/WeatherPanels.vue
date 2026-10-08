<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

defineProps<{ altitudeM: number }>()
type Field = 't2m_K' | 'u10_m_s' | 'v10_m_s' | 'msl_Pa' | 'tp_6h_mm'
type Choice = { init_time_utc: string; valid_time_utc: string; lead_hours: number }
type WeatherStatus = {
  state: string; source: string | null; message: string; options: Choice[]
  rows?: number; ready_for_route_risk: boolean
}
type Forecast = Choice & {
  historical: boolean
  point: { latitude_deg: number; longitude_deg: number }
  raw: Record<Field, number>; values: Record<Field, number>
  sources: Record<Field, 'ridge' | 'fuxi'>
  derived: { wind_speed_10m_m_s: number; wind_direction_from_deg: number | null }
  model_warning: string | null
  point_risk: {
    level: string; label: string; message: string; route_reason: string
    triggers: { field: string; label: string; value: number; unit: string; threshold: number }[]
    rules: { wind_attention_m_s: number; wind_elevated_m_s: number; rain_attention_6h_mm: number; rain_elevated_6h_mm: number }
  }
}

const status = ref<WeatherStatus | null>(null)
const forecast = ref<Forecast | null>(null)
const selectedKey = ref('')
const loading = ref(false)
const error = ref('')
let activeController: AbortController | null = null
const metrics: { field: Field; label: string }[] = [
  { field: 't2m_K', label: '温度 · °C' },
  { field: 'u10_m_s', label: 'U 风 · m/s' },
  { field: 'v10_m_s', label: 'V 风 · m/s' },
  { field: 'msl_Pa', label: '气压 · hPa' },
  { field: 'tp_6h_mm', label: '降水 · mm/6h' },
]
const weatherLabel = computed(() => (
  loading.value ? '加载中' : forecast.value ? '历史单点已接入' : '等待数据'
))
const modelPolicy = computed(() => forecast.value?.model_warning
  ? '模型不可用，所有变量当前使用 FuXi 原值。'
  : '温度、U 风、降水订正；V 风、气压保留 FuXi 原值。')

function choiceKey(choice: Choice) { return `${choice.init_time_utc}|${choice.lead_hours}` }
function localTime(time: string) {
  return new Date(time).toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false,
  })
}
function metricValue(field: Field, value: number) {
  if (field === 't2m_K') value -= 273.15
  if (field === 'msl_Pa') value /= 100
  return value.toFixed(2)
}
async function jsonResponse(response: Response) {
  const payload = await response.json()
  if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : '天气接口暂时不可用。')
  return payload
}
function startRequest() {
  activeController?.abort()
  const controller = new AbortController()
  activeController = controller
  loading.value = true
  error.value = ''
  forecast.value = null
  return controller
}
async function requestForecast(controller: AbortController) {
  const choice = status.value?.options.find(option => choiceKey(option) === selectedKey.value)
  if (!choice) throw new Error('所选预报时间不可用，请刷新天气数据。')
  const params = new URLSearchParams({ init_time_utc: choice.init_time_utc, lead_hours: String(choice.lead_hours) })
  const response = await fetch(`/api/weather/forecast?${params}`, { signal: controller.signal })
  const payload: Forecast = await jsonResponse(response)
  if (!controller.signal.aborted && controller === activeController) forecast.value = payload
}
function finishRequest(controller: AbortController, cause?: unknown) {
  if (controller.signal.aborted || controller !== activeController) return
  if (cause) error.value = cause instanceof Error ? cause.message : '天气数据加载失败。'
  loading.value = false
  activeController = null
}
async function refreshWeather() {
  const controller = startRequest()
  try {
    const payload: WeatherStatus = await jsonResponse(await fetch('/api/weather/status', { signal: controller.signal }))
    if (controller.signal.aborted || controller !== activeController) return
    status.value = payload
    if (!payload.options?.length) throw new Error(payload.message)
    if (!payload.options.some(option => choiceKey(option) === selectedKey.value)) {
      selectedKey.value = choiceKey(payload.options[0]!)
    }
    await requestForecast(controller)
    finishRequest(controller)
  } catch (cause) { finishRequest(controller, cause) }
}
async function changeForecast() {
  const controller = startRequest()
  try {
    await requestForecast(controller)
    finishRequest(controller)
  } catch (cause) { finishRequest(controller, cause) }
}

onMounted(() => { void refreshWeather() })
onBeforeUnmount(() => { activeController?.abort() })
</script>

<template>
  <div class="status-block weather-risk-panel" aria-label="气象风险">
    <div class="status-title"><span>气象风险</span><b :class="forecast ? `risk-${forecast.point_risk.level}` : 'pending'">{{ forecast?.point_risk.label || (loading ? '加载中' : '待评估') }}</b></div>
    <template v-if="forecast">
      <p v-if="forecast.point_risk.triggers.length === 0">该历史时次的地面风雨未触发演示阈值。</p>
      <ul v-else class="weather-triggers">
        <li v-for="trigger in forecast.point_risk.triggers" :key="trigger.field">{{ trigger.label }}：{{ trigger.value.toFixed(2) }} {{ trigger.unit }}（≥ {{ trigger.threshold }}）</li>
      </ul>
      <p class="weather-scope">{{ forecast.point_risk.route_reason }}</p>
      <small>任务高度 {{ altitudeM }} m；当前风数据仅为地面以上 10 m。</small>
      <details class="weather-rule-details"><summary>查看单点提示规则</summary><p>风速 ≥ {{ forecast.point_risk.rules.wind_attention_m_s }} / {{ forecast.point_risk.rules.wind_elevated_m_s }} m/s，6 小时降水 ≥ {{ forecast.point_risk.rules.rain_attention_6h_mm }} / {{ forecast.point_risk.rules.rain_elevated_6h_mm }} mm，分别触发关注 / 较强提示。</p><small>{{ forecast.point_risk.message }}</small></details>
    </template>
    <p v-else>{{ error || '正在读取天气数据；沿线天气仍待评估。' }}</p>
  </div>

  <div class="status-block weather-data-panel" aria-label="天气数据">
    <div class="status-title"><span>天气数据</span><b class="weather-state">{{ weatherLabel }}</b></div>
    <div class="weather-choice-row" v-if="status?.options?.length">
      <label for="weather-valid-time">预报有效时间 · 北京时间</label>
      <div class="weather-choice-controls"><select id="weather-valid-time" v-model="selectedKey" :disabled="loading" @change="changeForecast"><option v-for="option in status.options" :key="choiceKey(option)" :value="choiceKey(option)">{{ localTime(option.valid_time_utc) }} · +{{ option.lead_hours }}h</option></select><button type="button" class="weather-refresh" :disabled="loading" aria-label="刷新天气数据" @click="refreshWeather">刷新</button></div>
    </div>
    <p v-if="error" class="form-error" role="alert">{{ error }}</p>
    <template v-if="forecast">
      <small>点位 {{ forecast.point.latitude_deg }}°N，{{ forecast.point.longitude_deg }}°E · 历史预报</small>
      <table class="weather-values"><caption>FuXi 原值与最终使用值</caption><thead><tr><th scope="col">变量</th><th scope="col">FuXi</th><th scope="col">使用值</th></tr></thead><tbody><tr v-for="metric in metrics" :key="metric.field"><th scope="row">{{ metric.label }}<span class="weather-value-source">{{ forecast.sources[metric.field] === 'ridge' ? '订正' : 'FuXi 原值' }}</span></th><td>{{ metricValue(metric.field, forecast.raw[metric.field]) }}</td><td>{{ metricValue(metric.field, forecast.values[metric.field]) }}</td></tr></tbody></table>
      <div class="weather-derived"><span>10 米风速 <strong>{{ forecast.derived.wind_speed_10m_m_s.toFixed(2) }} m/s</strong></span><span>风向（来向）<strong>{{ forecast.derived.wind_direction_from_deg === null ? '静风' : `${forecast.derived.wind_direction_from_deg.toFixed(0)}°` }}</strong></span></div>
      <small>起报：{{ localTime(forecast.init_time_utc) }}（北京时间）· +{{ forecast.lead_hours }}h</small>
      <small class="weather-model-policy">{{ modelPolicy }}</small>
      <p v-if="forecast.model_warning" class="form-error" role="alert">{{ forecast.model_warning }}</p>
      <small>这是历史单点数据，不代表当前天气或沿线天气。</small>
    </template>
    <p v-else-if="!loading && !error">{{ status?.message || '天气数据尚未接入。' }}</p>
  </div>
</template>

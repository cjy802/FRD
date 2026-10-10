<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import type { WeatherLimits, WeatherRisk } from './weatherTypes'

const props = defineProps<{ jobId: string | null; coordinates: number[][] | null; departureBeijing: string; speedKmh: number; altitudeM: number; limits: WeatherLimits | null }>()
const emit = defineEmits<{ choose: [time: string]; state: [phase: string] }>()
type Candidate = { departure_time_utc: string; covered: boolean; selected: boolean; native_departure: boolean; level: string
  score_0_100: number | null; label?: string; reason: string; summary: WeatherRisk['summary'] | null }
type Windows = { state: string; candidates: Candidate[]; covered_count: number; message: string; ranking_basis: string }
const result = ref<Windows | null>(null)
const pending = ref(false)
const error = ref('')
let controller: AbortController | null = null
let timer: ReturnType<typeof setTimeout> | null = null
function beijing(time: string) { return new Date(time).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }) }
async function compare() {
  if (!props.jobId || !props.coordinates) return
  const current = new AbortController()
  controller = current
  pending.value = true
  emit('state', 'pending')
  try {
    const departure = new Date(`${props.departureBeijing}:00+08:00`)
    if (Number.isNaN(departure.getTime())) throw new Error('出发时间无效。')
    const response = await fetch(`/api/weather/predictions/${encodeURIComponent(props.jobId)}/windows`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: current.signal,
      body: JSON.stringify({ coordinates: props.coordinates, departure_time_utc: departure.toISOString(), speed_kmh: props.speedKmh, altitude_m: props.altitudeM, profile: props.limits }),
    })
    const payload = await response.json()
    if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : '出发时刻比较失败。')
    if (current.signal.aborted || current !== controller) return
    result.value = payload
    emit('state', payload.state)
  } catch (cause) {
    if (!current.signal.aborted && current === controller) { error.value = cause instanceof Error ? cause.message : '时次比较失败。'; emit('state', 'failed') }
  } finally { if (current === controller) { pending.value = false; controller = null } }
}
watch(() => [props.jobId, props.coordinates, props.departureBeijing, props.speedKmh, props.altitudeM, props.limits], () => {
  controller?.abort(); controller = null; if (timer) clearTimeout(timer)
  result.value = null; pending.value = false; error.value = ''; emit('state', 'waiting')
  if (props.jobId && props.coordinates) timer = setTimeout(() => { void compare() }, 350)
}, { immediate: true, deep: true })
onBeforeUnmount(() => { controller?.abort(); if (timer) clearTimeout(timer) })
</script>

<template>
  <div class="status-block" aria-label="出发时刻选择">
    <div class="status-title"><span>出发时刻选择</span><b class="pending">{{ pending ? '比较中' : result ? `${result.covered_count} 个可比较` : '待比较' }}</b></div>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-else-if="!result">{{ pending ? '正在用已有天气比较出发时次…' : '等待当前道路路线与区域天气。' }}</p>
    <template v-else>
      <p class="weather-choice-current">当前选择：{{ beijing(props.departureBeijing + ':00+08:00') }}（北京时间）</p>
      <small>出发时刻由左侧输入框控制。下面的时间只是资料较完整的参考选项，不会自动替换当前选择。</small>
      <details class="weather-rule-details"><summary>查看可比较的其他出发时间（{{ result.covered_count }} 个）</summary><ol class="weather-window-list"><li v-for="candidate in result.candidates" :key="candidate.departure_time_utc"><strong>{{ beijing(candidate.departure_time_utc) }}（北京时间）</strong><span>{{ candidate.selected ? '当前时间' : candidate.covered ? '资料完整，可比较' : '资料不足，暂不能评估' }}</span><span>{{ candidate.covered ? candidate.label : candidate.reason }}</span><button type="button" class="weather-refresh" :disabled="!candidate.covered || candidate.selected" @click="emit('choose', candidate.departure_time_utc)">{{ candidate.selected ? '当前时间' : '采用这个时间' }}</button></li></ol><small>{{ result.ranking_basis }}</small><small>{{ result.message }}</small></details>
    </template>
  </div>
</template>

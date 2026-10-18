<script setup lang="ts">
import { computed, onUnmounted, ref } from 'vue'
import {
  PhotoAnalysisApiError,
  fetchPhotoAnalysisHealth,
  fetchPhotoAnalysisJob,
  photoAnalysisFileUrl,
  submitPhotoAnalysis,
} from './photoAnalysis'
import type { PhotoAnalysisHealth, PhotoAnalysisJob } from './photoAnalysis'

const health = ref<PhotoAnalysisHealth | null>(null)
const healthError = ref('')
const selectedModel = ref('')
const selectedFiles = ref<File[]>([])
const siteName = ref('')
const note = ref('')
const job = ref<PhotoAnalysisJob | null>(null)
const busy = ref(false)
const error = ref('')
const status = ref('')
const viewMode = ref<'overlay' | 'original'>('overlay')
let pollTimer: number | null = null

const selectedModelOption = computed(() => health.value?.models?.find(model => model.id === selectedModel.value) ?? null)
const canSubmit = computed(() => Boolean(health.value?.available && selectedModelOption.value?.available
  && selectedFiles.value.length && !busy.value))
const interpretation = computed(() => job.value?.results?.interpretation ?? '')
const modelSummary = computed(() => {
  const model = job.value?.results?.model
  if (!model) return ''
  return `${model.display_name ?? model.default_model} · 输入 ${model.input_size}px`
})

function fractionPercent(value: number): string {
  return `${(value * 100).toFixed(1)}%`
}

async function refreshHealth() {
  healthError.value = ''
  try {
    health.value = await fetchPhotoAnalysisHealth()
    if (!selectedModelOption.value?.available) selectedModel.value = health.value.default_model_id
  } catch (cause) {
    health.value = null
    healthError.value = cause instanceof PhotoAnalysisApiError ? cause.message : '无法连接照片分析服务。'
  }
}

function onFilesChanged(event: Event) {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  const max = health.value?.max_photos_per_job ?? 200
  selectedFiles.value = files.slice(0, max)
  if (files.length > max) status.value = `一次最多 ${max} 张，已截取前 ${max} 张。`
}

async function submit() {
  if (!canSubmit.value) return
  busy.value = true
  error.value = ''
  status.value = '正在上传照片…'
  stopPolling()
  job.value = null
  try {
    const submitted = await submitPhotoAnalysis(selectedFiles.value, {
      site_name: siteName.value.trim() || undefined,
      note: note.value.trim() || undefined,
    }, selectedModel.value)
    status.value = '已提交，正在分析…'
    pollJob(submitted.job_id)
  } catch (cause) {
    busy.value = false
    status.value = ''
    error.value = cause instanceof PhotoAnalysisApiError ? cause.message : '上传失败，请重试。'
  }
}

function pollJob(jobId: string) {
  pollTimer = window.setTimeout(async () => {
    try {
      const current = await fetchPhotoAnalysisJob(jobId)
      job.value = current
      if (current.state === 'done') {
        busy.value = false
        status.value = `分析完成：${current.results?.images.length ?? 0} 张照片，用时 ${current.results?.elapsed_seconds ?? 0} 秒。`
        return
      }
      if (current.state === 'error') {
        busy.value = false
        status.value = ''
        error.value = current.error || '分析失败。'
        return
      }
      pollJob(jobId)
    } catch (cause) {
      busy.value = false
      status.value = ''
      error.value = cause instanceof PhotoAnalysisApiError ? cause.message : '查询分析进度失败。'
    }
  }, 1500)
}

function stopPolling() {
  if (pollTimer !== null) {
    window.clearTimeout(pollTimer)
    pollTimer = null
  }
}

function fileUrl(name: string): string {
  return job.value ? photoAnalysisFileUrl(job.value.job_id, name) : ''
}

function inputUrl(index: number): string {
  const record = job.value?.inputs?.[index]
  return record ? photoAnalysisFileUrl(job.value!.job_id, `inputs/${record.name}`) : ''
}

refreshHealth()
onUnmounted(stopPolling)
</script>

<template>
  <details class="photo-panel" @toggle="($event.target as HTMLDetailsElement).open && refreshHealth()">
    <summary>
      <span class="photo-heading">飞行后照片分析</span>
      <span v-if="health" :class="['photo-state', health.available ? 'is-ok' : 'is-off']">
        {{ health.available ? (health.busy ? '分析中' : '就绪') : '未就绪' }}
      </span>
    </summary>
    <div class="photo-panel-body">
      <p v-if="healthError" class="photo-feedback is-error" role="alert">{{ healthError }}</p>
      <p v-else-if="health && !health.available" class="photo-feedback is-error" role="alert">{{ health.message }}</p>

      <div class="photo-toolbar">
        <small>上传无人机拍摄的照片，自动分割疑似积水并生成叠图；结果仅提示待核查位置。</small>
        <button type="button" aria-label="刷新照片分析状态" @click="refreshHealth">刷新</button>
      </div>

      <div class="photo-form">
        <label for="photo-analysis-model">分割模型</label>
        <select id="photo-analysis-model" v-model="selectedModel" class="photo-model-select" :disabled="busy || !health?.available">
          <option v-for="model in health?.models ?? []" :key="model.id" :value="model.id" :disabled="!model.available">
            {{ model.label }}{{ model.available ? '' : '（未就绪）' }}
          </option>
        </select>
        <small v-if="selectedModelOption" class="photo-model-description">{{ selectedModelOption.description }}</small>
        <label for="photo-analysis-files">照片（可多选）</label>
        <input id="photo-analysis-files" type="file" accept="image/jpeg,image/png,image/webp,image/bmp" multiple :disabled="busy || !health?.available" @change="onFilesChanged" />
        <div class="photo-context">
          <input v-model="siteName" type="text" maxlength="60" autocomplete="off" placeholder="站点/地点（可选）" :disabled="busy" />
          <input v-model="note" type="text" maxlength="120" autocomplete="off" placeholder="备注（可选）" :disabled="busy" />
        </div>
        <p v-if="selectedFiles.length" class="photo-count">{{ selectedFiles.length }} 张已选择</p>
        <button type="button" class="photo-submit" :disabled="!canSubmit" @click="submit">
          {{ busy ? '分析中…' : '开始分析' }}
        </button>
      </div>

      <p v-if="error" class="photo-feedback is-error" role="alert">{{ error }}</p>
      <p v-else-if="status" class="photo-feedback" role="status">{{ status }}</p>

      <div v-if="job?.results" class="photo-results">
        <div class="photo-results-header">
          <div class="photo-view-toggle" role="group" aria-label="叠图显示方式">
            <button type="button" :class="{ 'is-active': viewMode === 'overlay' }" @click="viewMode = 'overlay'">叠图</button>
            <button type="button" :class="{ 'is-active': viewMode === 'original' }" @click="viewMode = 'original'">原图</button>
          </div>
          <small>{{ modelSummary }}</small>
        </div>
        <ul class="photo-grid">
          <li v-for="(image, index) in job.results.images" :key="image.name">
            <a :href="viewMode === 'overlay' ? fileUrl(image.overlay) : inputUrl(index)" target="_blank" rel="noopener">
              <img :src="viewMode === 'overlay' ? fileUrl(image.overlay) : inputUrl(index)" :alt="`疑似积水叠图 ${image.name}`" loading="lazy" />
            </a>
            <div class="photo-meta">
              <strong :title="image.name">{{ image.name }}</strong>
              <small>疑似积水 {{ fractionPercent(image.suspected_fraction) }} · {{ image.suspected_patch_count }} 处</small>
              <small>平均置信度 {{ fractionPercent(image.mean_confidence_on_suspected) }}</small>
            </div>
          </li>
        </ul>
        <p class="photo-warning">{{ interpretation }}</p>
      </div>
    </div>
  </details>
</template>

<style scoped>
.photo-panel { min-width: 0; margin: 12px 0; border: 1px solid var(--ui-line); border-radius: var(--ui-radius); background: #fff; font-size: var(--font-caption); }
.photo-panel > summary { display: flex; align-items: center; justify-content: space-between; gap: 6px; min-height: 38px; padding: 7px 10px; cursor: pointer; font-weight: 600; list-style-position: inside; }
.photo-panel > summary::marker { color: var(--ui-muted); }
.photo-state { display: inline-grid; place-items: center; min-width: 44px; height: 20px; padding: 0 6px; border-radius: 10px; font-size: 11px; }
.photo-state.is-ok { background: #e7f6ec; color: #16794c; }
.photo-state.is-off { background: #fdeaea; color: #a4262c; }
.photo-panel-body { min-width: 0; padding: 0 10px 10px; border-top: 1px solid var(--ui-line); }
.photo-panel-body button { min-height: 28px; padding: 4px 8px; border: 1px solid #d7dfe8; border-radius: 6px; background: #fff; color: #34506c; white-space: nowrap; font-size: 11px; }
.photo-panel-body button:hover:not(:disabled) { border-color: var(--ui-accent); color: var(--ui-accent); background: #eff6ff; }
.photo-panel-body button:disabled { opacity: .5; cursor: not-allowed; }
.photo-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 6px; padding: 8px 0; }
.photo-toolbar small { color: var(--ui-muted); line-height: 1.4; }
.photo-form { padding: 4px 0 10px; }
.photo-form > label { display: block; margin-bottom: 4px; color: #424242; font-weight: 600; }
.photo-model-select { display: block; width: 100%; min-width: 0; height: 32px; margin: 0 0 4px; padding: 4px 7px; border: 1px solid #d1d5db; border-radius: 6px; background: #f9fafb; color: #27364a; font-size: var(--font-caption); }
.photo-model-description { display: block; margin: 0 0 8px; color: var(--ui-muted); line-height: 1.4; }
.photo-form input[type='text'] { width: 100%; min-width: 0; height: 30px; padding: 4px 7px; border: 1px solid #d1d5db; border-radius: 6px; background: #f9fafb; font-size: var(--font-caption); }
.photo-context { display: grid; gap: 5px; margin-top: 6px; }
.photo-count { margin: 5px 0 0; color: var(--ui-muted); }
.photo-submit { margin-top: 8px; border-color: var(--ui-accent) !important; background: var(--ui-accent) !important; color: #fff !important; }
.photo-submit:hover:not(:disabled) { background: var(--ui-accent-hover) !important; }
.photo-feedback { margin: 0 0 8px; padding: 6px 8px; border-radius: 6px; background: #eff6ff; color: #1d4ed8; line-height: 1.4; overflow-wrap: anywhere; }
.photo-feedback.is-error { background: #fef2f2; color: #a4262c; }
.photo-results-header { display: flex; align-items: center; justify-content: space-between; gap: 6px; margin-bottom: 8px; }
.photo-results-header small { color: var(--ui-muted); overflow-wrap: anywhere; text-align: right; }
.photo-view-toggle { display: inline-flex; gap: 0; }
.photo-view-toggle button { border-radius: 0; }
.photo-view-toggle button:first-child { border-radius: 6px 0 0 6px; }
.photo-view-toggle button:last-child { border-radius: 0 6px 6px 0; border-left: none; }
.photo-view-toggle button.is-active { background: var(--ui-accent); border-color: var(--ui-accent); color: #fff; }
.photo-grid { margin: 0; padding: 0; list-style: none; display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 8px; }
.photo-grid li { min-width: 0; padding: 6px; border: 1px solid var(--ui-line); border-radius: 7px; background: #fbfcfe; }
.photo-grid img { display: block; width: 100%; height: auto; border-radius: 5px; }
.photo-meta { margin-top: 5px; }
.photo-meta strong { display: block; overflow: hidden; font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.photo-meta small { display: block; margin-top: 2px; color: var(--ui-muted); }
.photo-warning { margin: 10px 0 0; padding: 6px 8px; border-radius: 6px; background: #fff7ed; color: #9a5b13; line-height: 1.4; }
</style>

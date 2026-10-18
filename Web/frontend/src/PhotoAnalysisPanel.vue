<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import {
  PhotoAnalysisApiError,
  fetchPhotoAnalysisHealth,
  fetchPhotoAnalysisJob,
  photoAnalysisFileUrl,
  submitPhotoAnalysis,
} from './photoAnalysis'
import type { PhotoAnalysisHealth, PhotoAnalysisJob, PhotoAnalysisResultImage } from './photoAnalysis'
import type { TaskVideoFrame } from './photoAnalysis'

const props = defineProps<{ taskId: string | null; frames: TaskVideoFrame[] }>()
const emit = defineEmits<{ 'remove-frame': [index: number]; 'analysis-complete': [job: PhotoAnalysisJob] }>()

const health = ref<PhotoAnalysisHealth | null>(null)
const healthError = ref('')
const selectedModel = ref('')
const excludedFrames = ref<number[]>([])
const siteName = ref('')
const note = ref('')
const job = ref<PhotoAnalysisJob | null>(null)
const busy = ref(false)
const error = ref('')
const status = ref('')
const viewMode = ref<'overlay' | 'original'>('overlay')
const previewDialog = ref<HTMLDialogElement | null>(null)
const preview = ref<{ image: PhotoAnalysisResultImage; index: number } | null>(null)
let pollTimer: number | null = null

watch(() => props.frames.map(frame => `${frame.file.name}:${frame.time_seconds}`).join('|'), () => {
  excludedFrames.value = []
})

const selectedModelOption = computed(() => health.value?.models?.find(model => model.id === selectedModel.value) ?? null)
const isRemoteModel = computed(() => selectedModel.value === 'remote-glm-puddle')
const selectedFrames = computed(() => props.frames.filter((_, index) => !excludedFrames.value.includes(index)))
const selectedFiles = computed(() => selectedFrames.value.map(frame => frame.file))
const canSubmit = computed(() => Boolean(health.value?.available && selectedModelOption.value?.available
  && props.taskId && selectedFiles.value.length && !busy.value))
const interpretation = computed(() => job.value?.results?.interpretation ?? '')
const selectedFileSummary = computed(() => {
  if (selectedFiles.value.length === 1) return selectedFiles.value[0].name
  if (selectedFiles.value.length > 1) return `${selectedFiles.value.length} 个视频关键帧已选择`
  return '先从任务视频中截取需要分析的关键帧'
})
const previewUrl = computed(() => {
  if (!preview.value) return ''
  return viewMode.value === 'overlay'
    ? fileUrl(preview.value.image.overlay)
    : inputUrl(preview.value.index)
})
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

function removeFrame(index: number) {
  excludedFrames.value = [...excludedFrames.value, index]
  emit('remove-frame', index)
}

function setFrameIncluded(index: number, event: Event) {
  const checked = (event.target as HTMLInputElement).checked
  excludedFrames.value = checked
    ? excludedFrames.value.filter(item => item !== index)
    : excludedFrames.value.includes(index) ? excludedFrames.value : [...excludedFrames.value, index]
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
    }, selectedModel.value, props.taskId ?? '', selectedFrames.value.map(({ file, ...metadata }) => metadata))
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
        emit('analysis-complete', current)
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

function openPreview(image: PhotoAnalysisResultImage, index: number) {
  preview.value = { image, index }
  void nextTick(() => {
    if (previewDialog.value && !previewDialog.value.open) previewDialog.value.showModal()
  })
}

function closePreview() {
  if (previewDialog.value?.open) previewDialog.value.close()
  else preview.value = null
}

function onPreviewDialogClick(event: MouseEvent) {
  if (event.target === previewDialog.value) closePreview()
}

function onPreviewDialogClose() {
  preview.value = null
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
        <small>只分析当前任务视频中手动截取的关键帧；SRT 按时间提供无人机位置，不逐帧调用模型。</small>
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
        <p v-if="isRemoteModel" class="photo-feedback">所选任务关键帧会发送到 BigModel 接口处理；API Key 只保存在本机后端。返回多边形会在本机生成掩膜。</p>
        <label>任务关键帧（{{ selectedFiles.length }} / {{ frames.length }}）</label>
        <p v-if="!taskId" class="photo-file-selection">先在“选定航线”中创建当前任务。</p>
        <p v-else-if="!frames.length" class="photo-file-selection">{{ selectedFileSummary }}</p>
        <ul v-else class="photo-frame-list">
          <li v-for="(frame, index) in frames" :key="`${frame.file.name}-${index}`" :class="{ 'is-excluded': excludedFrames.includes(index) }">
            <label><input type="checkbox" :checked="!excludedFrames.includes(index)" :disabled="busy" @change="setFrameIncluded(index, $event)" />
              <span><strong>{{ frame.file.name }}</strong><small>{{ Math.floor(frame.time_seconds / 60).toString().padStart(2, '0') }}:{{ Math.floor(frame.time_seconds % 60).toString().padStart(2, '0') }} · {{ frame.latitude.toFixed(6) }}, {{ frame.longitude.toFixed(6) }}<template v-if="frame.altitude_m !== null"> · {{ frame.altitude_m.toFixed(0) }} m</template></small></span>
            </label>
            <button type="button" :disabled="busy" :aria-label="`从任务分析队列移除 ${frame.file.name}`" @click="removeFrame(index)">移除</button>
          </li>
        </ul>
        <div class="photo-context">
          <input v-model="siteName" type="text" maxlength="60" autocomplete="off" placeholder="站点/地点（可选）" :disabled="busy" />
          <input v-model="note" type="text" maxlength="120" autocomplete="off" placeholder="备注（可选）" :disabled="busy" />
        </div>
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
            <button type="button" class="photo-thumbnail" :aria-label="`放大查看 ${image.name}`" @click="openPreview(image, index)">
              <img :src="viewMode === 'overlay' ? fileUrl(image.overlay) : inputUrl(index)" :alt="`${viewMode === 'overlay' ? '疑似积水叠图' : '原始照片'} ${image.name}`" loading="lazy" />
            </button>
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
    <dialog v-if="preview" ref="previewDialog" class="photo-lightbox" :aria-labelledby="`photo-preview-title-${preview.index}`" @click="onPreviewDialogClick" @close="onPreviewDialogClose">
      <div class="photo-lightbox-content">
        <header class="photo-lightbox-header">
          <div class="photo-lightbox-heading">
            <strong :id="`photo-preview-title-${preview.index}`">{{ preview.image.name }}</strong>
            <small>{{ viewMode === 'overlay' ? '分析叠图' : '原始照片' }}</small>
          </div>
          <div class="photo-lightbox-actions">
            <div class="photo-view-toggle" role="group" aria-label="预览图片类型">
              <button type="button" :class="{ 'is-active': viewMode === 'overlay' }" @click="viewMode = 'overlay'">叠图</button>
              <button type="button" :class="{ 'is-active': viewMode === 'original' }" @click="viewMode = 'original'">原图</button>
            </div>
            <button type="button" class="photo-lightbox-close" aria-label="关闭图片预览" @click="closePreview">关闭</button>
          </div>
        </header>
        <div class="photo-lightbox-body">
          <img class="photo-lightbox-image" :src="previewUrl" :alt="`${viewMode === 'overlay' ? '分析叠图' : '原始照片'} ${preview.image.name}`" />
        </div>
      </div>
    </dialog>
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
.photo-file-selection { min-width: 0; overflow: hidden; color: var(--ui-muted); text-overflow: ellipsis; white-space: nowrap; }
.photo-frame-list { display: grid; max-height: 200px; gap: 5px; margin: 5px 0; padding: 0; overflow: auto; list-style: none; }
.photo-frame-list li { display: flex; align-items: center; justify-content: space-between; gap: 5px; padding: 5px 6px; border: 1px solid var(--ui-line); border-radius: 6px; }
.photo-frame-list li.is-excluded { opacity: .55; }
.photo-frame-list label { display: flex; min-width: 0; align-items: flex-start; gap: 6px; margin: 0; cursor: pointer; }
.photo-frame-list input { margin: 2px 0 0; accent-color: var(--ui-accent); }
.photo-frame-list span { display: grid; min-width: 0; gap: 2px; }
.photo-frame-list strong, .photo-frame-list small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.photo-frame-list strong { font-size: 11px; }
.photo-frame-list small { color: var(--ui-muted); font-size: 10px; }
.photo-form input[type='text'] { width: 100%; min-width: 0; height: 30px; padding: 4px 7px; border: 1px solid #d1d5db; border-radius: 6px; background: #f9fafb; font-size: var(--font-caption); }
.photo-context { display: grid; gap: 5px; margin-top: 6px; }
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
.photo-thumbnail { display: block; width: 100%; padding: 0 !important; border: none !important; border-radius: 5px; background: transparent !important; cursor: zoom-in; }
.photo-thumbnail:focus-visible { outline: 2px solid var(--ui-accent); outline-offset: 2px; }
.photo-grid img { display: block; width: 100%; height: auto; border-radius: 5px; }
.photo-meta { margin-top: 5px; }
.photo-meta strong { display: block; overflow: hidden; font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.photo-meta small { display: block; margin-top: 2px; color: var(--ui-muted); }
.photo-warning { margin: 10px 0 0; padding: 6px 8px; border-radius: 6px; background: #fff7ed; color: #9a5b13; line-height: 1.4; }
.photo-lightbox { width: min(96vw, 1400px); max-width: 96vw; max-height: 94vh; margin: auto; padding: 0; overflow: hidden; border: 1px solid var(--ui-line); border-radius: var(--ui-radius); background: #fff; color: #27364a; }
.photo-lightbox::backdrop { background: rgba(15, 23, 42, .48); }
.photo-lightbox-content { display: flex; max-height: 94vh; flex-direction: column; }
.photo-lightbox-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 12px; border-bottom: 1px solid var(--ui-line); background: #fff; }
.photo-lightbox-heading { display: grid; min-width: 0; gap: 3px; }
.photo-lightbox-heading strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.photo-lightbox-heading small { color: var(--ui-muted); }
.photo-lightbox-actions { display: flex; flex: 0 0 auto; align-items: center; gap: 10px; }
.photo-lightbox button { min-height: 28px; padding: 4px 8px; border: 1px solid #d7dfe8; border-radius: 6px; background: #fff; color: #34506c; white-space: nowrap; font-size: 11px; }
.photo-lightbox button:hover:not(:disabled) { border-color: var(--ui-accent); color: var(--ui-accent); background: #eff6ff; }
.photo-lightbox button:disabled { opacity: .5; cursor: not-allowed; }
.photo-lightbox-body { display: grid; min-height: 0; place-items: center; padding: 0 12px 12px; }
.photo-lightbox-image { display: block; max-width: 100%; max-height: calc(94vh - 66px); object-fit: contain; }
@media (max-width: 560px) {
  .photo-lightbox { width: 96vw; }
  .photo-lightbox-header { align-items: flex-start; flex-direction: column; }
  .photo-lightbox-actions { width: 100%; justify-content: space-between; }
  .photo-lightbox-image { max-height: calc(94vh - 108px); }
}
</style>

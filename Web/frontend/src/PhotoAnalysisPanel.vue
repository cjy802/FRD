<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import {
  PhotoAnalysisApiError,
  assessWaterAlert,
  cancelPhotoAnalysisJob,
  fetchPhotoAnalysisHealth,
  fetchPhotoAnalysisJob,
  photoAnalysisFileUrl,
  retryFailedPhotoAnalysisFrames,
  sanitizeWaterAlertThresholds,
  submitPhotoAnalysisJob,
} from './photoAnalysis'
import type { PhotoAnalysisHealth, PhotoAnalysisJob, PhotoAnalysisJobItem, PhotoAnalysisResultImage, WaterAlertThresholds } from './photoAnalysis'
import type { TaskVideoFrame } from './photoAnalysis'

const props = withDefaults(defineProps<{ taskId: string | null; frames: TaskVideoFrame[]; thresholds: WaterAlertThresholds; activeJobId?: string | null; queueOpen?: boolean }>(), { activeJobId: null, queueOpen: false })
const emit = defineEmits<{ 'remove-frame': [index: number]; 'frames-add': [frames: TaskVideoFrame[]]; 'job-started': [jobId: string]; 'analysis-update': [job: PhotoAnalysisJob]; 'thresholds-change': [thresholds: WaterAlertThresholds]; 'open-queue': []; 'close-queue': [] }>()

const health = ref<PhotoAnalysisHealth | null>(null)
const healthError = ref('')
const selectedModel = ref('')
const modelMenuOpen = ref(false)
const modelPicker = ref<HTMLElement | null>(null)
const modelTrigger = ref<HTMLButtonElement | null>(null)
const modelOptionRefs = ref<HTMLButtonElement[]>([])
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
const photoInput = ref<HTMLInputElement | null>(null)
let pollTimer: number | null = null

const localThresholds = ref<WaterAlertThresholds>({ ...props.thresholds })
watch(() => props.thresholds, value => { localThresholds.value = { ...value } })
watch(localThresholds, value => emit('thresholds-change', sanitizeWaterAlertThresholds(value)), { deep: true })

watch(() => props.frames.map(frame => `${frame.file.name}:${frame.time_seconds}`).join('|'), () => {
  excludedFrames.value = []
  if (status.value.endsWith('加入任务关键帧…')) status.value = ''
})

const selectedModelOption = computed(() => health.value?.models?.find(model => model.id === selectedModel.value) ?? null)
const availableModelIndexes = computed(() => (health.value?.models ?? []).flatMap((model, index) => model.available ? [index] : []))
const isRemoteModel = computed(() => selectedModel.value === 'remote-glm-puddle')
const selectedFrames = computed(() => props.frames.filter((_, index) => !excludedFrames.value.includes(index)))
const selectedFiles = computed(() => selectedFrames.value.map(frame => frame.file))
const canSubmit = computed(() => Boolean(health.value?.available && selectedModelOption.value?.available
  && props.taskId && selectedFiles.value.length && !busy.value))
const interpretation = computed(() => job.value?.results?.interpretation ?? '')
const selectedFileSummary = computed(() => {
  if (selectedFiles.value.length === 1) return selectedFiles.value[0].name
  if (selectedFiles.value.length > 1) return `${selectedFiles.value.length} 个关键帧已选择`
  return '从任务视频截取关键帧，或选择本机照片'
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
const imageAssessments = computed(() => (job.value?.results?.images ?? [])
  .map(image => ({ image, assessment: assessWaterAlert(image, props.thresholds) })))
const alertSummary = computed(() => {
  const counts = { elevated: 0, attention: 0 }
  for (const { assessment } of imageAssessments.value) {
    if (assessment.level === 'elevated') counts.elevated += 1
    else if (assessment.level === 'attention') counts.attention += 1
  }
  return counts
})

function fractionPercent(value: number): string {
  return `${(value * 100).toFixed(1)}%`
}

function setModelOptionRef(element: unknown, index: number) {
  if (element instanceof HTMLButtonElement) modelOptionRefs.value[index] = element
}

function focusModelOption(index: number) {
  void nextTick(() => modelOptionRefs.value[index]?.focus())
}

function openModelMenu() {
  if (busy.value || !health.value?.available || !availableModelIndexes.value.length) return
  modelMenuOpen.value = true
  const selectedIndex = health.value.models.findIndex(model => model.id === selectedModel.value && model.available)
  focusModelOption(selectedIndex >= 0 ? selectedIndex : availableModelIndexes.value[0])
}

function closeModelMenu(restoreFocus = false) {
  modelMenuOpen.value = false
  if (restoreFocus) void nextTick(() => modelTrigger.value?.focus())
}

function toggleModelMenu() {
  if (modelMenuOpen.value) closeModelMenu()
  else openModelMenu()
}

function chooseModel(modelId: string) {
  const model = health.value?.models?.find(item => item.id === modelId)
  if (!model?.available || busy.value) return
  selectedModel.value = modelId
  closeModelMenu(true)
}

function focusAdjacentModel(currentIndex: number, direction: 1 | -1) {
  const indexes = availableModelIndexes.value
  if (!indexes.length) return
  const position = indexes.indexOf(currentIndex)
  const nextPosition = position < 0
    ? (direction > 0 ? 0 : indexes.length - 1)
    : (position + direction + indexes.length) % indexes.length
  focusModelOption(indexes[nextPosition])
}

function onModelTriggerKeydown(event: KeyboardEvent) {
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault()
    if (!modelMenuOpen.value) openModelMenu()
    else {
      const selectedIndex = health.value?.models.findIndex(model => model.id === selectedModel.value && model.available) ?? -1
      focusAdjacentModel(selectedIndex, event.key === 'ArrowDown' ? 1 : -1)
    }
  } else if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault()
    toggleModelMenu()
  } else if (event.key === 'Escape' && modelMenuOpen.value) {
    event.preventDefault()
    closeModelMenu()
  }
}

function onModelOptionKeydown(event: KeyboardEvent, index: number) {
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault()
    focusAdjacentModel(index, event.key === 'ArrowDown' ? 1 : -1)
  } else if (event.key === 'Home' || event.key === 'End') {
    event.preventDefault()
    const indexes = availableModelIndexes.value
    if (indexes.length) focusModelOption(event.key === 'Home' ? indexes[0] : indexes[indexes.length - 1])
  } else if (event.key === 'Escape') {
    event.preventDefault()
    closeModelMenu(true)
  } else if (event.key === 'Tab') {
    closeModelMenu()
  }
}

function onDocumentPointerDown(event: PointerEvent) {
  if (modelMenuOpen.value && !modelPicker.value?.contains(event.target as Node)) closeModelMenu()
}

function onPhotoPanelToggle(event: Event) {
  if ((event.target as HTMLDetailsElement).open) void refreshHealth()
  else closeModelMenu()
}

function onModelPickerFocusout(event: FocusEvent) {
  if (!modelPicker.value?.contains(event.relatedTarget as Node | null)) closeModelMenu()
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

function choosePhotos() { photoInput.value?.click() }

function onPhotosPicked(event: Event) {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files ?? []).filter(file => /\.(jpe?g|png|webp)$/i.test(file.name) || ['image/jpeg', 'image/png', 'image/webp'].includes(file.type))
  input.value = ''
  if (!files.length || !props.taskId) return
  emit('frames-add', files.map(file => ({
    file,
    time_seconds: 0,
    latitude: null,
    longitude: null,
    altitude_m: null,
    selection_reason: '手动上传',
  })))
  status.value = files.length > 1 ? `正在把 ${files.length} 张照片加入任务关键帧…` : '正在把照片加入任务关键帧…'
}

function setFrameIncluded(index: number, event: Event) {
  const checked = (event.target as HTMLInputElement).checked
  excludedFrames.value = checked
    ? excludedFrames.value.filter(item => item !== index)
    : excludedFrames.value.includes(index) ? excludedFrames.value : [...excludedFrames.value, index]
}

async function submit() {
  if (!canSubmit.value) return
  const frameIds = selectedFrames.value.map(frame => frame.frame_id).filter((id): id is string => Boolean(id))
  if (!frameIds.length) {
    error.value = '所选关键帧还没有保存到任务，稍等片刻后重试。'
    return
  }
  busy.value = true
  error.value = ''
  status.value = '已提交，排队识别中…'
  stopPolling()
  job.value = null
  try {
    const submitted = await submitPhotoAnalysisJob({
      taskId: props.taskId ?? '',
      frameIds,
      modelId: selectedModel.value,
      siteName: siteName.value.trim() || undefined,
      note: note.value.trim() || undefined,
      cameraFovDeg: props.thresholds.cameraFovDeg,
    })
    emit('job-started', submitted.job_id)
    pollJob(submitted.job_id)
  } catch (cause) {
    busy.value = false
    status.value = ''
    error.value = cause instanceof PhotoAnalysisApiError ? cause.message : '提交失败，请重试。'
  }
}

let pollSignature = ''

function pollJob(jobId: string) {
  pollTimer = window.setTimeout(async () => {
    try {
      const current = await fetchPhotoAnalysisJob(jobId)
      job.value = current
      const counts = current.counts
      const signature = `${current.state}:${counts?.total ?? 0}:${counts?.done ?? 0}:${counts?.failed ?? 0}`
      if (signature !== pollSignature) {
        pollSignature = signature
        if (current.mode === 'frames') emit('analysis-update', current)
      }
      if (current.state === 'done') {
        busy.value = false
        const done = counts?.done ?? current.results?.images.length ?? 0
        const failed = counts?.failed ?? 0
        status.value = `分析完成：成功 ${done} 张${failed ? ` · 失败 ${failed} 张（可重试失败帧）` : ''}，用时 ${Math.round(current.results?.elapsed_seconds ?? 0)} 秒。`
        return
      }
      if (current.state === 'error') {
        busy.value = false
        status.value = ''
        error.value = current.error || '分析失败。'
        return
      }
      if (current.state === 'cancelled') {
        busy.value = false
        const done = counts?.done ?? 0
        status.value = `已取消分析：完成 ${done} 张，结果已保留。`
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

async function attachJob(jobId: string) {
  if (!jobId || (pollTimer !== null && job.value?.job_id === jobId)) return
  try {
    const current = await fetchPhotoAnalysisJob(jobId)
    job.value = current
    pollSignature = ''
    if (current.state === 'pending' || current.state === 'running') {
      busy.value = true
      status.value = '任务在后台识别中，进度如下。'
      pollJob(jobId)
    } else if (current.mode === 'frames') {
      emit('analysis-update', current)
    }
  } catch {
    // 任务可能已随任务删除；静默忽略，不打断面板。
  }
}

watch(() => props.activeJobId, jobId => { if (jobId) void attachJob(jobId) }, { immediate: true })

async function cancelAnalysis() {
  if (!job.value || job.value.state !== 'running' || !busy.value) return
  try {
    await cancelPhotoAnalysisJob(job.value.job_id)
  } catch (cause) {
    error.value = cause instanceof PhotoAnalysisApiError ? cause.message : '取消失败，请重试。'
  }
}

async function retryFailed() {
  if (!job.value) return
  busy.value = true
  error.value = ''
  status.value = '正在重新排队识别失败帧…'
  try {
    await retryFailedPhotoAnalysisFrames(job.value.job_id)
    pollSignature = ''
    pollJob(job.value.job_id)
  } catch (cause) {
    busy.value = false
    status.value = ''
    error.value = cause instanceof PhotoAnalysisApiError ? cause.message : '重试失败帧失败。'
  }
}

const queueItems = computed<PhotoAnalysisJobItem[]>(() => job.value?.items ?? [])
const queueCounts = computed(() => job.value?.counts ?? { total: 0, done: 0, failed: 0, pending: 0, running: 0, cancelled: 0 })
const queueSummaryText = computed(() => {
  const counts = queueCounts.value
  if (!counts.total) return ''
  const waiting = counts.pending + counts.running
  const parts = [`已识别 ${counts.done} / 共 ${counts.total}`]
  if (counts.failed) parts.push(`失败 ${counts.failed}`)
  if (waiting) parts.push(`排队中 ${waiting}`)
  if (job.value?.closed === false) parts.push('仍可追加关键帧')
  return parts.join(' · ')
})
const canCancelJob = computed(() => busy.value && job.value?.state === 'running')
const canRetryFailed = computed(() => !busy.value && Boolean(job.value && job.value.counts?.failed))
const etaText = computed(() => {
  const counts = queueCounts.value
  const elapsed = job.value?.results?.elapsed_seconds ?? 0
  if (job.value?.state !== 'running' || isRemoteModel.value !== true) return ''
  if (counts.done < 2 || !elapsed) return ''
  const remaining = Math.max(0, Math.round(elapsed / counts.done * (counts.total - counts.done - counts.failed)))
  if (remaining < 5) return ''
  const minutes = Math.floor(remaining / 60)
  const seconds = remaining % 60
  return `预计剩余 ${minutes ? `${minutes} 分 ` : ''}${seconds} 秒（按已识别帧估算）`
})

function segmentWidth(kind: 'done' | 'failed' | 'running'): string {
  const counts = queueCounts.value
  if (!counts.total) return '0%'
  return `${(counts[kind] / counts.total) * 100}%`
}

function itemStateText(item: PhotoAnalysisJobItem): string {
  if (item.state === 'pending') return '排队'
  if (item.state === 'running') return item.attempts > 0 ? `重试 ${item.attempts}` : '识别中'
  if (item.state === 'done') return '完成'
  if (item.state === 'failed') return '失败'
  return '取消'
}

const thumbUrls = new Map<string, string>()
const frameIdByItemName = computed(() => {
  const map = new Map<string, string>()
  for (const item of queueItems.value) map.set(item.name, item.frame_id)
  return map
})

function queueThumbUrl(item: PhotoAnalysisJobItem): string {
  const cached = thumbUrls.get(item.frame_id)
  if (cached) return cached
  const frame = props.frames.find(candidate => candidate.frame_id === item.frame_id)
  if (!frame) return ''
  const url = URL.createObjectURL(frame.file)
  thumbUrls.set(item.frame_id, url)
  return url
}

function releaseThumbUrls() {
  for (const url of thumbUrls.values()) URL.revokeObjectURL(url)
  thumbUrls.clear()
}

watch(() => props.frames.length, () => releaseThumbUrls())

function openQueueItemPreview(item: PhotoAnalysisJobItem) {
  if (item.state !== 'done' || !job.value) return
  const index = (job.value.results?.images ?? []).findIndex(image => image.name === item.name)
  if (index < 0) return
  openPreview((job.value.results?.images ?? [])[index], index)
}

function stopPolling() {
  if (pollTimer !== null) {
    window.clearTimeout(pollTimer)
    pollTimer = null
  }
}

// —— 队列浮层：和视频文件夹浏览一样铺满 #route-map 地图矩形 ——
const queueMapRect = ref<{ top: number; left: number; width: number; height: number } | null>(null)

function updateQueueMapRect() {
  const region = document.querySelector('#route-map') as HTMLElement | null
  if (!region) return
  const rect = region.getBoundingClientRect()
  queueMapRect.value = { top: rect.top, left: rect.left, width: rect.width, height: rect.height }
}

const queueDockStyle = computed(() => {
  const rect = queueMapRect.value
  if (!rect) return {}
  return { top: `${rect.top}px`, left: `${rect.left}px`, width: `${rect.width}px`, height: `${rect.height}px` }
})

watch(() => props.queueOpen, open => {
  if (open) void nextTick(updateQueueMapRect)
})

function fileUrl(name: string): string {
  return job.value ? photoAnalysisFileUrl(job.value.job_id, name) : ''
}

function inputUrl(index: number): string {
  const current = job.value
  if (!current) return ''
  const record = current.inputs?.[index]
  if (record) return photoAnalysisFileUrl(current.job_id, `inputs/${record.name}`)
  const image = current.results?.images?.[index]
  const frameId = image ? frameIdByItemName.value.get(image.name) : undefined
  if (!frameId) return ''
  const frame = props.frames.find(candidate => candidate.frame_id === frameId)
  if (!frame) return ''
  const cached = thumbUrls.get(frameId)
  if (cached) return cached
  const url = URL.createObjectURL(frame.file)
  thumbUrls.set(frameId, url)
  return url
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
onMounted(() => {
  document.addEventListener('pointerdown', onDocumentPointerdown)
  window.addEventListener('resize', updateQueueMapRect)
})
onUnmounted(() => {
  stopPolling()
  releaseThumbUrls()
  document.removeEventListener('pointerdown', onDocumentPointerdown)
  window.removeEventListener('resize', updateQueueMapRect)
})

function onDocumentPointerdown(event: PointerEvent) {
  onDocumentPointerDown(event)
}
</script>

<template>
  <details class="photo-panel" @toggle="onPhotoPanelToggle">
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
        <small>只分析勾选的任务关键帧。关键帧可从任务视频截取，也可直接选择本机照片；没有拍摄高度的帧不换算积水面积。</small>
        <button type="button" aria-label="刷新照片分析状态" @click="refreshHealth">刷新</button>
      </div>

      <div class="photo-form">
        <span id="photo-analysis-model-label" class="photo-model-label">分割模型</span>
        <div ref="modelPicker" class="photo-model-picker" @focusout="onModelPickerFocusout">
          <button id="photo-analysis-model" ref="modelTrigger" type="button" role="combobox" class="photo-model-select" :disabled="busy || !health?.available" :aria-labelledby="'photo-analysis-model-label'" aria-haspopup="listbox" aria-controls="photo-analysis-model-options" :aria-expanded="modelMenuOpen" @click="toggleModelMenu" @keydown="onModelTriggerKeydown">
            <span class="photo-model-value">{{ selectedModelOption?.label ?? '选择分割模型' }}</span><span class="photo-model-chevron" :class="{ 'is-open': modelMenuOpen }" aria-hidden="true"></span>
          </button>
          <div v-if="modelMenuOpen" id="photo-analysis-model-options" class="photo-model-options" role="listbox" aria-labelledby="photo-analysis-model-label">
          <button v-for="(model, index) in health?.models ?? []" :key="model.id" :ref="element => setModelOptionRef(element, index)" type="button" role="option" class="photo-model-option" :class="{ 'is-selected': model.id === selectedModel }" :disabled="!model.available" :aria-selected="model.id === selectedModel" @click="chooseModel(model.id)" @keydown="onModelOptionKeydown($event, index)">
              <span>{{ model.label }}</span><span v-if="model.id === selectedModel" class="photo-model-check" aria-hidden="true">✓</span><span v-else-if="!model.available" class="photo-model-unavailable">未就绪</span>
            </button>
          </div>
        </div>
        <small v-if="selectedModelOption" class="photo-model-description">{{ selectedModelOption.description }}</small>
        <p v-if="isRemoteModel" class="photo-feedback">所选任务关键帧会发送到 BigModel 接口处理；API Key 只保存在本机后端。返回多边形会在本机生成掩膜。</p>
        <details class="weather-threshold-config photo-threshold-config" aria-label="积水警报阈值配置">
          <summary class="weather-threshold-config-heading"><strong>设置积水警报阈值</strong><small>结合拍摄高度换算面积，达标才警报</small></summary>
          <div class="weather-threshold-group">
            <div class="weather-threshold-item"><strong>警报面积阈值 · m²</strong><label>最大单块水面达到该面积触发警报<input v-model.number="localThresholds.minAlertAreaM2" type="number" min="0.5" max="100000" step="0.5" :disabled="busy" /></label><small>总面积达标但只是破碎小水坑时仅提示“注意”</small></div>
            <div class="weather-threshold-item"><strong>相机水平视场角 · °</strong><label>按拍摄机型校准（Air 3 约 75）<input v-model.number="localThresholds.cameraFovDeg" type="number" min="30" max="120" step="1" :disabled="busy" /></label><small>面积 = 疑似像素 × GSD²，GSD 由该帧高度与视场角换算</small></div>
          </div>
          <small class="weather-threshold-note">面积是“平坦地面 + 近垂直俯拍”假设下的估算；SRT 高度相对起飞点，斜拍帧（云台俯角不足）精度低、只降级为“注意”。面积不代表水深或淹没范围，仍需现场核查。</small>
        </details>
        <div class="photo-frame-heading">
          <label>任务关键帧（{{ selectedFiles.length }} / {{ frames.length }}）</label>
          <input ref="photoInput" class="photo-file-native" type="file" accept="image/jpeg,image/png,image/webp,.jpg,.jpeg,.png,.webp" multiple :disabled="busy || !taskId" @change="onPhotosPicked" />
          <button type="button" class="photo-file-trigger" :disabled="busy || !taskId" @click="choosePhotos">选择文件</button>
        </div>
        <p v-if="!taskId" class="photo-file-selection">先在“选定航线”中创建当前任务。</p>
        <p v-else-if="!frames.length" class="photo-file-selection">{{ selectedFileSummary }}</p>
        <ul v-else class="photo-frame-list">
          <li v-for="(frame, index) in frames" :key="`${frame.file.name}-${index}`" :class="{ 'is-excluded': excludedFrames.includes(index) }">
            <label><input type="checkbox" :checked="!excludedFrames.includes(index)" :disabled="busy" @change="setFrameIncluded(index, $event)" />
              <span><strong>{{ frame.file.name }}</strong><small>{{ Math.floor(frame.time_seconds / 60).toString().padStart(2, '0') }}:{{ Math.floor(frame.time_seconds % 60).toString().padStart(2, '0') }}<template v-if="frame.latitude !== null && frame.longitude !== null"> · {{ frame.latitude.toFixed(6) }}, {{ frame.longitude.toFixed(6) }}</template><template v-else> · 无 GPS</template><template v-if="frame.altitude_m !== null"> · {{ frame.altitude_m.toFixed(0) }} m</template></small><small>{{ frame.selection_reason ?? '手动截取' }}<template v-if="frame.estimated_overlap_percent !== null && frame.estimated_overlap_percent !== undefined"> · 上一选帧重叠约 {{ frame.estimated_overlap_percent }}%</template><template v-if="frame.sharpness_ratio_percent !== null && frame.sharpness_ratio_percent !== undefined"> · 清晰度比值 {{ frame.sharpness_ratio_percent }}%</template></small></span>
            </label>
            <button type="button" :disabled="busy" :aria-label="`从任务分析队列移除 ${frame.file.name}`" @click="removeFrame(index)">移除</button>
          </li>
        </ul>
        <small v-if="taskId" class="photo-file-hint">选择文件支持多选 JPG、PNG、WebP；手动照片没有 GPS 和高度，分析时不换算面积、不触发积水警报。</small>
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

      <div v-if="job && job.mode === 'frames'" class="analysis-queue-inline">
        <small>{{ queueSummaryText || '识别队列已就绪' }}</small>
        <span>
          <button v-if="canCancelJob" type="button" @click="cancelAnalysis">取消分析</button>
          <button type="button" @click="emit('open-queue')">打开识别队列</button>
        </span>
      </div>

      <div v-if="job?.results" class="photo-results">
        <div class="photo-results-header">
          <div class="photo-view-toggle" role="group" aria-label="叠图显示方式">
            <button type="button" :class="{ 'is-active': viewMode === 'overlay' }" @click="viewMode = 'overlay'">叠图</button>
            <button type="button" :class="{ 'is-active': viewMode === 'original' }" @click="viewMode = 'original'">原图</button>
          </div>
          <small>{{ modelSummary }}</small>
        </div>
        <p :class="['photo-alert-summary', { 'is-elevated': alertSummary.elevated > 0 }]" role="status">
          {{ alertSummary.elevated || alertSummary.attention
            ? `积水警报 ${alertSummary.elevated} 帧 · 注意 ${alertSummary.attention} 帧`
            : '未触发积水警报' }}（阈值 {{ thresholds.minAlertAreaM2 }} m² · 视场角 {{ thresholds.cameraFovDeg }}°）
        </p>
        <ul class="photo-grid">
          <li v-for="{ image, assessment }, index in imageAssessments" :key="image.name">
            <button type="button" class="photo-thumbnail" :aria-label="`放大查看 ${image.name}`" @click="openPreview(image, index)">
              <img :src="viewMode === 'overlay' ? fileUrl(image.overlay) : inputUrl(index)" :alt="`${viewMode === 'overlay' ? '疑似积水叠图' : '原始照片'} ${image.name}`" loading="lazy" />
            </button>
            <div class="photo-meta">
              <strong :title="image.name">{{ image.name }}</strong>
              <span :class="['photo-alert-badge', `is-${assessment.level}`]">{{ assessment.label }}</span>
              <small v-if="assessment.areaText">{{ assessment.areaText }} · 高度 {{ image.altitude_m !== null && image.altitude_m !== undefined ? `${image.altitude_m.toFixed(0)} m` : '未知' }}</small>
              <small>疑似积水 {{ fractionPercent(image.suspected_fraction) }} · {{ image.suspected_patch_count }} 处 · 置信度 {{ fractionPercent(image.mean_confidence_on_suspected) }}</small>
              <small class="photo-alert-reason" :title="assessment.reason">{{ assessment.reason }}</small>
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

    <Teleport to="body">
      <div v-if="queueOpen && job && job.mode === 'frames'" class="analysis-queue-overlay" :style="queueDockStyle" role="dialog" aria-label="识别队列进度">
        <header class="analysis-queue-overlay-head">
          <div class="analysis-queue-overlay-title">
            <strong>识别队列</strong>
            <small>{{ queueSummaryText || '等待关键帧入队' }}</small>
          </div>
          <div class="analysis-queue-overlay-actions">
            <button v-if="canCancelJob" type="button" @click="cancelAnalysis">取消分析</button>
            <button v-if="canRetryFailed" type="button" class="is-danger" @click="retryFailed">重试失败帧（{{ queueCounts.failed }}）</button>
            <button type="button" class="analysis-queue-overlay-close" aria-label="关闭识别队列" @click="emit('close-queue')">关闭</button>
          </div>
        </header>
        <div class="analysis-queue-overlay-body">
          <div class="analysis-queue-progress" role="progressbar" :aria-valuenow="queueCounts.done + queueCounts.failed" :aria-valuemax="queueCounts.total" aria-label="已识别帧数">
            <span class="is-done" :style="{ width: segmentWidth('done') }"></span>
            <span class="is-failed" :style="{ width: segmentWidth('failed') }"></span>
            <span class="is-running" :style="{ width: segmentWidth('running') }"></span>
          </div>
          <small v-if="etaText" class="analysis-queue-eta">{{ etaText }}</small>
          <ul v-if="queueItems.length" class="analysis-queue-grid">
            <li v-for="item in queueItems" :key="item.frame_id" :class="`is-${item.state}`" :title="item.error ?? item.name">
              <button v-if="item.state === 'done'" type="button" class="analysis-queue-thumb" :aria-label="`查看 ${item.name} 的识别结果`" @click="openQueueItemPreview(item)">
                <img v-if="queueThumbUrl(item)" :src="queueThumbUrl(item)" :alt="item.name" loading="lazy" />
                <span v-else class="analysis-queue-blank" aria-hidden="true">已识别</span>
                <span class="analysis-queue-state">{{ itemStateText(item) }}</span>
              </button>
              <div v-else class="analysis-queue-thumb is-static" :title="item.error ?? item.name">
                <img v-if="queueThumbUrl(item)" :src="queueThumbUrl(item)" :alt="item.name" loading="lazy" />
                <span v-else class="analysis-queue-blank" aria-hidden="true">无预览</span>
                <span class="analysis-queue-state">{{ itemStateText(item) }}</span>
              </div>
            </li>
          </ul>
          <p v-else class="analysis-queue-empty">还没有关键帧入队；截帧开始后这里会逐张显示识别进度。</p>
        </div>
      </div>
    </Teleport>
  </details>
</template>

<style scoped>
.analysis-queue-inline { display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-wrap: wrap; margin: 8px 0 10px; padding: 6px 8px; border: 1px solid var(--ui-line); border-radius: 7px; background: #fbfcfe; }
.analysis-queue-inline small { min-width: 0; flex: 1 1 120px; color: #526273; font-size: 11px; line-height: 1.4; }
.analysis-queue-inline > span { display: inline-flex; gap: 6px; }
.analysis-queue-overlay { position: fixed; z-index: 6000; display: flex; flex-direction: column; overflow: hidden; border: 1px solid var(--ui-line); border-radius: 10px; background: #f4f7fa; box-shadow: 0 14px 40px rgb(15 23 42 / .24); }
.analysis-queue-overlay-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 10px 14px; border-bottom: 1px solid var(--ui-line); background: #fff; }
.analysis-queue-overlay-title { display: grid; min-width: 0; gap: 2px; }
.analysis-queue-overlay-title strong { font-size: 13px; color: #173b5c; }
.analysis-queue-overlay-title small { color: var(--ui-muted); font-size: 11px; }
.analysis-queue-overlay-actions { display: flex; align-items: center; gap: 6px; }
.analysis-queue-overlay-actions button { min-height: 30px; padding: 4px 10px; border: 1px solid #d7dfe8; border-radius: 6px; background: #fff; color: #34506c; white-space: nowrap; font-size: 11px; cursor: pointer; }
.analysis-queue-overlay-actions button:hover:not(:disabled) { border-color: var(--ui-accent); color: var(--ui-accent); background: #eff6ff; }
.analysis-queue-overlay-actions button:disabled { opacity: .5; cursor: not-allowed; }
.analysis-queue-overlay-actions .is-danger { border-color: #d5a3a3; color: #a4262c; background: #fff7f7; }
.analysis-queue-overlay-actions .analysis-queue-overlay-close { font-weight: 700; }
.analysis-queue-overlay-body { min-height: 0; flex: 1; overflow-y: auto; padding: 12px 14px; }
.analysis-queue-empty { margin: 0; color: var(--ui-muted); font-size: 12px; line-height: 1.6; }
.analysis-queue-progress { display: flex; gap: 1px; height: 10px; border-radius: 5px; overflow: hidden; background: #e3e9f0; }
.analysis-queue-progress > span { height: 100%; transition: width .4s ease; }
.analysis-queue-progress .is-done { background: #2f9e63; }
.analysis-queue-progress .is-failed { background: #d64545; }
.analysis-queue-progress .is-running { background: #4a90d9; animation: analysis-queue-pulse 1.4s ease-in-out infinite; }
@keyframes analysis-queue-pulse { 0%, 100% { opacity: .55; } 50% { opacity: 1; } }
.analysis-queue-eta { display: block; margin-top: 6px; color: var(--ui-muted); font-size: 11px; }
.analysis-queue-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(104px, 1fr)); gap: 8px; margin: 12px 0 0; padding: 0; list-style: none; }
.analysis-queue-grid li { min-width: 0; }
.analysis-queue-thumb { position: relative; display: block; width: 100%; padding: 0 !important; border: 1px solid var(--ui-line) !important; border-radius: 6px; background: #fff !important; overflow: hidden; cursor: default; }
.analysis-queue-thumb:not(.is-static) { cursor: zoom-in; }
.analysis-queue-thumb:focus-visible { outline: 2px solid var(--ui-accent); outline-offset: 1px; }
.analysis-queue-thumb img { display: block; width: 100%; aspect-ratio: 4 / 3; object-fit: cover; }
.analysis-queue-blank { display: grid; place-items: center; width: 100%; aspect-ratio: 4 / 3; color: var(--ui-muted); font-size: 10px; background: #eef2f6; }
.analysis-queue-state { position: absolute; left: 0; bottom: 0; padding: 2px 6px; border-radius: 0 4px 0 0; background: rgb(23 36 49 / .72); color: #fff; font-size: 10px; line-height: 1.5; white-space: nowrap; }
.analysis-queue-grid .is-done .analysis-queue-state { background: rgb(31 125 74 / .9); }
.analysis-queue-grid .is-failed .analysis-queue-thumb { border-color: #e3b3b3 !important; }
.analysis-queue-grid .is-failed .analysis-queue-state { background: rgb(180 50 50 / .92); }
.analysis-queue-grid .is-running .analysis-queue-thumb { border-color: #a8ccef !important; }
.analysis-queue-grid .is-running .analysis-queue-state { background: rgb(35 100 168 / .92); animation: analysis-queue-pulse 1.4s ease-in-out infinite; }
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
.photo-model-label { display: block; margin-bottom: 4px; color: #424242; font-weight: 600; }
.photo-model-picker { position: relative; z-index: 12; width: 100%; margin: 0 0 4px; }
.photo-panel-body .photo-model-select { display: flex; width: 100%; min-width: 0; min-height: 32px; align-items: center; justify-content: space-between; gap: 10px; padding: 5px 10px; border: 1px solid #cbd5df; border-radius: 7px; background: #f9fafb; color: #27364a; font: inherit; font-size: var(--font-caption); text-align: left; }
.photo-panel-body .photo-model-select:hover:not(:disabled) { transform: none; border-color: #9fc5e4; background: #fff; box-shadow: none; color: #27364a; }
.photo-panel-body .photo-model-select:focus-visible { border-color: var(--ui-accent); outline: 2px solid var(--ui-accent); outline-offset: 2px; box-shadow: 0 0 0 2px #fff, 0 0 0 4px var(--ui-accent); }
.photo-model-select:disabled { opacity: .55; cursor: not-allowed; }
.photo-model-value { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.photo-model-chevron { width: 8px; height: 8px; flex: 0 0 auto; margin: -4px 2px 0 0; border-right: 2px solid #64748b; border-bottom: 2px solid #64748b; transform: rotate(45deg); transition: transform .16s ease, margin .16s ease; }
.photo-model-chevron.is-open { margin-top: 4px; transform: rotate(225deg); }
.photo-model-options { position: absolute; z-index: 1; top: calc(100% + 4px); left: 0; right: 0; display: grid; max-height: 260px; gap: 3px; padding: 4px; overflow-y: auto; border: 1px solid #cbd5df; border-radius: 8px; background: #fff; box-shadow: 0 8px 22px rgb(15 23 42 / .18); }
.photo-panel-body .photo-model-options .photo-model-option { display: flex; width: 100%; min-height: 34px; align-items: center; justify-content: space-between; gap: 10px; padding: 6px 9px; border: 1px solid transparent; border-radius: 5px; background: #fff; color: #344455; font-size: var(--font-caption); text-align: left; white-space: normal; }
.photo-panel-body .photo-model-options .photo-model-option:hover:not(:disabled), .photo-panel-body .photo-model-options .photo-model-option:focus-visible { transform: none; border-color: #d6e7f5; background: #f2f8fd; box-shadow: none; color: #1769aa; outline: none; }
.photo-panel-body .photo-model-options .photo-model-option.is-selected { border-color: #c5def2; background: #e6f1fb; color: #126cb7; font-weight: 700; }
.photo-panel-body .photo-model-options .photo-model-option:disabled { opacity: .55; cursor: not-allowed; }
.photo-model-check { flex: 0 0 auto; color: #087b76; font-weight: 800; }
.photo-model-unavailable { flex: 0 0 auto; color: var(--ui-muted); font-size: 10px; }
.photo-model-description { display: block; margin: 0 0 8px; color: var(--ui-muted); line-height: 1.4; }
.photo-frame-heading { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.photo-frame-heading label { margin: 0; }
.photo-file-native { display: none; }
.photo-file-trigger { min-height: 28px; padding: 4px 10px; border: 1px solid #d7dfe8; border-radius: 6px; background: #fff; color: #34506c; font-size: 11px; white-space: nowrap; }
.photo-file-trigger:hover:not(:disabled) { border-color: var(--ui-accent); background: #eff6ff; color: var(--ui-accent); }
.photo-file-trigger:disabled { opacity: .5; cursor: not-allowed; }
.photo-file-hint { display: block; margin: 0 0 6px; color: var(--ui-muted); line-height: 1.4; }
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
.photo-threshold-config { margin: 0 0 10px; }
.photo-alert-summary { margin: 0 0 8px; padding: 6px 8px; border-radius: 6px; background: #f0f7f1; color: #16794c; line-height: 1.4; }
.photo-alert-summary.is-elevated { background: #fef2f2; color: #a4262c; font-weight: 700; }
.photo-alert-badge { display: inline-block; margin-top: 3px; padding: 2px 7px; border-radius: 9px; font-size: 10px; font-weight: 700; }
.photo-alert-badge.is-none { background: #eef2f6; color: #526273; }
.photo-alert-badge.is-attention { background: #fff7ed; color: #b45309; }
.photo-alert-badge.is-elevated { background: #fef2f2; color: #a4262c; }
.photo-alert-reason { overflow-wrap: anywhere; }
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

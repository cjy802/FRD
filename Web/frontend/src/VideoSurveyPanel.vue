<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import CustomSelect from './CustomSelect.vue'
import InfoHint from './InfoHint.vue'
import {
  createVideoSurveySrt,
  deleteVideoCaptureTarget,
  interpolateVideoPosition,
  selectAdaptiveTaskFrames,
} from './videoSurvey'
import type { VideoCaptureTarget, VideoSurvey, VideoTelemetryPoint } from './videoSurvey'
import type { PhotoAnalysisJob, PhotoAnalysisModelOption, TaskVideoFrame } from './photoAnalysis'
import { appendPhotoAnalysisFrames, closePhotoAnalysisJob, fetchPhotoAnalysisHealth, submitPhotoAnalysisJob } from './photoAnalysis'
import { PIPELINE_STAGES, onPipelineEvent, pipelineState, startChain } from './analysisPipeline'
import { saveMissionTaskFrame } from './missionTasks'
import { ExtractionCancelled, extractAdaptiveFramesClient, extractFixedIntervalFramesClient } from './adaptiveFrames'
import type { ClientAdaptiveProgress, ClientSelectedFrame } from './adaptiveFrames'
import {
  countSrtTelemetry,
  formatBytes,
  formatClock,
  formatDateTime,
  formatDurationBetween,
  groupVideoFiles,
  makeGroupThumbnail,
} from './videoLibrary'
import type { VideoLibraryGroup } from './videoLibrary'

const props = defineProps<{ taskId: string | null; frameCount: number }>()

const emit = defineEmits<{
  'track-change': [track: number[][]]
  'position-change': [position: VideoTelemetryPoint | null]
  'targets-change': [targets: VideoCaptureTarget[]]
  'frame-capture': [frame: TaskVideoFrame]
  'frame-saved': [frame: TaskVideoFrame]
  'survey-change': [survey: VideoSurvey | null]
  'job-started': [jobId: string]
  'open-queue': []
}>()

const survey = ref<VideoSurvey | null>(null)
const videoElement = ref<HTMLVideoElement | null>(null)
const currentPosition = ref<VideoTelemetryPoint | null>(null)
const currentTime = ref(0)
const videoDuration = ref(0)
const loading = ref(false)
const busyTarget = ref(false)
const selectingFrames = ref(false)
// 默认"边挨着边"：相邻帧零重叠，不重复浪费识别；需要留冗余时在界面调回。
const straightOverlapPercent = ref(0)
const turnOverlapPercent = ref(0)
const scanFps = ref(2)
const turnAngleDegrees = ref(18)
const cameraChangeDegrees = ref(12)
const blurFilterEnabled = ref(true)
const minimumSharpnessPercent = ref(40)
// 无 GPS 时的固定抽帧间隔（秒）；有 GPS 时仍走零重叠/转弯算法。
const fixedIntervalSeconds = ref(3)
const error = ref('')
const status = ref('')

// —— 视频文件夹浏览（面包屑：地图 › 文件夹 › 视频）——
const folderInput = ref<HTMLInputElement | null>(null)
const libraryOpen = ref(false)
const libraryFolderName = ref('')
const libraryGroups = ref<VideoLibraryGroup[]>([])
const selectedGroup = ref<VideoLibraryGroup | null>(null)
const previewGroup = ref<VideoLibraryGroup | null>(null)
const thumbnails = ref<Record<string, string>>({})
const srtCounts = ref<Record<string, number>>({})
const playSpeed = ref(1)
const previewVideo = ref<HTMLVideoElement | null>(null)
const previewUrl = ref('')
const mapRect = ref<{ top: number; left: number; width: number; height: number } | null>(null)
let previewUrlObject = ''
// 当前任务绑定的本地视频（SRT-only 导入后，预览/截帧都基于它）。
const localVideo = ref<{ file: File; url: string } | null>(null)
const extractingLocal = ref(false)
const extractionProgress = ref<ClientAdaptiveProgress | null>(null)
const extractionCancel = ref(false)

// —— 截帧即入队：抽帧阶段每选出一张就上传并入队识别 ——
const queueModels = ref<PhotoAnalysisModelOption[]>([])
const queueHealthLoaded = ref(false)
// 固定两级流水线（YOLO 全量初筛 → GLM 终审，双模型确认；ABCD 初筛已实测否决）。
// 推进与轮询在 analysisPipeline 全局单例里做（页面级生命周期、快照 taskId）：
// 本面板只负责启动与状态展示，切任务/切模块不断链；页面关闭后切回任务自动续跑。
const queueStages = ref<Array<{ id: string; short: string; noun: string }>>([])
const queueSkipped = ref(0)

async function ensureQueueModels() {
  if (queueHealthLoaded.value) return
  const health = await fetchPhotoAnalysisHealth()
  queueModels.value = health.models.filter(model => model.available)
  const available = new Set(queueModels.value.map(model => model.id))
  queueStages.value = PIPELINE_STAGES.filter(stage => available.has(stage.id))
  queueHealthLoaded.value = true
}

const canQueueAnalyze = computed(() => Boolean(canSelectAdaptiveFrames.value && localVideo.value && queueStages.value.length))
const queueStatusLabel = computed(() => {
  if (!pipelineState.running || pipelineState.taskId !== props.taskId) return ''
  const parts = [`已入队 ${pipelineState.total}`, `已识别 ${pipelineState.done}`]
  if (pipelineState.failed) parts.push(`失败 ${pipelineState.failed}`)
  if (queueSkipped.value) parts.push(`上传跳过 ${queueSkipped.value}`)
  const stage = queueStages.value[pipelineState.stageIndex]
  const prefix = pipelineState.stageIndex > 0 && stage ? `${stage.short} · ` : ''
  return `${prefix}${parts.join(' · ')}`
})

// 链在全局单例里结束，面板通过事件把结论落到本地状态条；taskId 不匹配（用户已切走）
// 时静默忽略，切回任务的恢复由 App.vue 的续跑检查负责。
let offPipelineEvents: (() => void) | null = null
function bindPipelineEvents() {
  offPipelineEvents?.()
  offPipelineEvents = onPipelineEvent(event => {
    if (event.taskId !== props.taskId) return
    if (event.type === 'chain-finished') {
      const images = event.job.results?.images ?? []
      const confirmed = images.filter(image => (image.suspected_pixels ?? 0) > 0).length
      const singleOnly = Math.max(0, (event.job.counts?.done ?? images.length) - confirmed)
      const confirmWord = queueStages.value.length >= 2 ? '双模型确认' : '模型确认'
      status.value = event.early
        ? 'YOLO 初筛完成：未检出疑似积水帧，流水线提前结束。'
        : `识别流水线完成：${confirmWord}疑似积水 ${confirmed} 帧${singleOnly ? `（另有 ${singleOnly} 帧仅 YOLO 检出，未确认）` : ''}；确认帧已生成地图积水目标，含经纬度与所在道路位置。`
    } else if (event.type === 'chain-stopped') {
      status.value = `识别流水线中断：${event.reason}。切回本任务会自动续跑。`
    }
  })
}

const reviewVideoSrc = computed(() => {
  if (localVideo.value) return localVideo.value.url
  if (survey.value?.has_video) return survey.value.video_url ?? ''
  return ''
})

// 浏览层在地图区域内铺满：以地图区域的矩形做定位。
const dockStyle = computed(() => {
  const rect = mapRect.value
  if (!rect) return {}
  return { top: `${rect.top}px`, left: `${rect.left}px`, width: `${rect.width}px`, height: `${rect.height}px` }
})

function updateMapRect() {
  const region = document.querySelector('#route-map') as HTMLElement | null
  if (!region) return
  const rect = region.getBoundingClientRect()
  mapRect.value = { top: rect.top, left: rect.left, width: rect.width, height: rect.height }
}

// 保险上限与后端 TaskStore.MAX_FRAMES_PER_TASK 保持一致；截图只增不删，满额后需手动移除旧帧。
const FRAME_LIMIT = 300

// 无 GPS 也能自动截帧：退化为固定时间间隔（坐标为空，地图不显示轨迹）。
const canSelectAdaptiveFrames = computed(() => Boolean(props.taskId && survey.value
  && props.frameCount < FRAME_LIMIT && !selectingFrames.value && !loading.value && !extractingLocal.value))
const hasGpsTelemetry = computed(() => Boolean(survey.value?.has_gps))
const adaptiveButtonLabel = computed(() => {
  if (extractingLocal.value) {
    const progress = extractionProgress.value
    return progress ? `本地自动截帧中 ${progress.scanned}/${progress.total} · 已选 ${progress.selected}…` : '正在本地自动截帧…'
  }
  if (selectingFrames.value) return '正在自适应筛帧…'
  if (props.frameCount >= FRAME_LIMIT) return '关键帧已达上限'
  if (survey.value && !survey.value.has_gps) return '按固定间隔自动截帧（无 GPS）'
  if (survey.value && !survey.value.has_video) return '自动筛选关键帧（本地处理）'
  return '自动筛选并保存关键帧'
})
const currentTimeLabel = computed(() => formatVideoTime(currentTime.value))
const previewImportBlockedReason = computed(() => {
  if (!props.taskId) return '请先创建当前任务'
  return ''
})
const canImportPreview = computed(() => Boolean(props.taskId && previewGroup.value && !loading.value))
// 无 SRT 导入采用两步确认：第一次点击只亮出提示条，确认后才真正导入。
const noSrtImportArmed = ref(false)
watch(previewGroup, () => { noSrtImportArmed.value = false })

function placeholderSrtFor(videoName: string): File {
  // 后端要求 SRT 非空；没有真实字幕时合成一个不含 GPS 的占位 SRT，
  // 走既有的 has_gps=false 链路（地图无轨迹、关键帧不能带 GPS 位置）。
  const content = `1\n00:00:00,000 --> 00:00:01,000\n[no-srt] ${videoName} 导入时未提供 SRT 字幕\n\n`
  return new File([content], `${videoName}.no-srt.srt`, { type: 'text/plain' })
}
const canCapturePreview = computed(() => Boolean(previewGroup.value && survey.value && previewVideo.value?.videoWidth
  && props.frameCount < 100 && !capturingPreview.value))

function formatVideoTime(seconds: number): string {
  const value = Math.max(0, Math.floor(seconds || 0))
  return `${String(Math.floor(value / 60)).padStart(2, '0')}:${String(value % 60).padStart(2, '0')}`
}

function compactTrack(points: VideoTelemetryPoint[]): number[][] {
  if (!points.length) return []
  const stride = Math.max(1, Math.ceil(points.length / 1000))
  const selected = points.filter((_, index) => index % stride === 0)
  if (selected[selected.length - 1] !== points[points.length - 1]) selected.push(points[points.length - 1])
  return selected.map(point => [point.longitude, point.latitude, point.altitude_m ?? 2])
}

function publishSurvey(value: VideoSurvey | null) {
  survey.value = value
  emit('survey-change', value)
  emit('track-change', value ? compactTrack(value.telemetry) : [])
  emit('targets-change', value?.targets ?? [])
  updatePosition()
}

function updatePosition(time = videoElement.value?.currentTime ?? currentTime.value) {
  currentTime.value = Number.isFinite(time) ? time : 0
  currentPosition.value = survey.value
    ? interpolateVideoPosition(survey.value.telemetry, currentTime.value)
    : null
  emit('position-change', currentPosition.value)
}

async function importVideo(srt: File, localFile: File, withoutSrt = false) {
  if (!props.taskId || loading.value) return
  loading.value = true
  error.value = ''
  status.value = withoutSrt ? '正在导入视频（无 SRT）…' : '正在读取 SRT 轨迹…'
  try {
    const result = await createVideoSurveySrt(srt, localFile.name, props.taskId)
    if (localVideo.value) URL.revokeObjectURL(localVideo.value.url)
    localVideo.value = { file: localFile, url: URL.createObjectURL(localFile) }
    publishSurvey(result)
    status.value = withoutSrt
      ? '视频已导入（未提供 SRT）：地图上不会显示飞行路径，关键帧也不会带 GPS 位置。'
      : result.has_gps
        ? `已导入 SRT 轨迹（本任务之前的视频已自动替换清理）；视频保留在本机，预览与截帧直接使用本地文件，不会上传。`
        : 'SRT 已导入，但没有识别到 GPS 坐标。请确认使用的是与此视频对应、含 GPS 的视频字幕文件。'
    if (videoElement.value) videoElement.value.currentTime = 0
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : 'SRT 导入失败。'
    status.value = ''
  } finally {
    loading.value = false
  }
}

function chooseVideoFolder() {
  updateMapRect()
  folderInput.value?.click()
}

function openLibraryBrowser() {
  updateMapRect()
  libraryOpen.value = true
}

function onFolderPicked(event: Event) {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  input.value = ''
  if (!files.length) return
  const groups = groupVideoFiles(files)
  libraryFolderName.value = files[0]?.webkitRelativePath?.split('/')[0] || '已选文件夹'
  libraryGroups.value = groups
  selectedGroup.value = null
  closePreview()
  thumbnails.value = {}
  srtCounts.value = {}
  libraryOpen.value = true
  error.value = ''
  status.value = ''
  void loadGroupPreviews(groups)
}

async function loadGroupPreviews(groups: VideoLibraryGroup[]) {
  // 有限并发：大文件夹几十段素材不能同时开几十路视频解码。
  const concurrency = 4
  let cursor = 0
  const workers = Array.from({ length: Math.min(concurrency, groups.length) }, async () => {
    while (cursor < groups.length) {
      const group = groups[cursor++]
      // 重新选择文件夹后，旧队列立即失效。
      if (libraryGroups.value !== groups) return
      const thumbnail = await makeGroupThumbnail(group)
      if (libraryGroups.value !== groups) return
      if (thumbnail) thumbnails.value = { ...thumbnails.value, [group.key]: thumbnail }
      if (group.srt && srtCounts.value[group.key] === undefined) {
        const count = await countSrtTelemetry(group.srt)
        if (libraryGroups.value !== groups) return
        srtCounts.value = { ...srtCounts.value, [group.key]: count }
      }
    }
  })
  await Promise.all(workers)
}

function openPreview(group: VideoLibraryGroup) {
  selectedGroup.value = group
  previewGroup.value = group
  playSpeed.value = 1
  if (previewUrlObject) URL.revokeObjectURL(previewUrlObject)
  previewUrlObject = URL.createObjectURL(group.video)
  previewUrl.value = previewUrlObject
}

function closePreview() {
  previewGroup.value = null
  if (previewUrlObject) URL.revokeObjectURL(previewUrlObject)
  previewUrlObject = ''
  previewUrl.value = ''
}

function closeLibrary() {
  closePreview()
  libraryOpen.value = false
}

async function importPreviewGroup() {
  const group = previewGroup.value
  if (!group || !props.taskId || loading.value) return
  if (!group.srt && !noSrtImportArmed.value) {
    noSrtImportArmed.value = true
    return
  }
  noSrtImportArmed.value = false
  await importVideo(group.srt ?? placeholderSrtFor(group.video.name), group.video, !group.srt)
  if (!error.value) closeLibrary()
}

function onLibraryKeydown(event: KeyboardEvent) {
  if (event.key !== 'Escape' || !libraryOpen.value) return
  event.stopPropagation()
  if (previewGroup.value) closePreview()
  else libraryOpen.value = false
}

const capturingPreview = ref(false)
async function capturePreviewFrame() {
  const video = previewVideo.value
  const group = previewGroup.value
  if (!video || !group || !video.videoWidth || !survey.value || capturingPreview.value) return
  capturingPreview.value = true
  error.value = ''
  try {
    const time = video.currentTime
    const point = interpolateVideoPosition(survey.value.telemetry, time)
    if (!point) throw new Error('此时刻没有 SRT 位置记录，无法作为分析关键帧。')
    const canvas = document.createElement('canvas')
    const scale = Math.min(1, 1920 / Math.max(video.videoWidth, video.videoHeight))
    canvas.width = Math.max(1, Math.round(video.videoWidth * scale))
    canvas.height = Math.max(1, Math.round(video.videoHeight * scale))
    const context = canvas.getContext('2d')
    if (!context) throw new Error('浏览器无法读取视频画面。')
    context.drawImage(video, 0, 0, canvas.width, canvas.height)
    const blob = await new Promise<Blob>((resolve, reject) => canvas.toBlob(value => value ? resolve(value) : reject(new Error('截取视频帧失败。')), 'image/jpeg', 0.92))
    const totalSeconds = Math.max(0, Math.floor(time))
    const name = `frame-${String(Math.floor(totalSeconds / 60)).padStart(2, '0')}m${String(totalSeconds % 60).padStart(2, '0')}s.jpg`
    emit('frame-capture', { file: new File([blob], name, { type: 'image/jpeg' }), time_seconds: time,
      latitude: point.latitude, longitude: point.longitude, altitude_m: point.altitude_m, gimbal_pitch_deg: point.gimbal_pitch_deg })
    status.value = `已把 ${formatVideoTime(time)} 的画面加入当前任务分析队列。`
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '截取视频帧失败。'
  } finally {
    capturingPreview.value = false
  }
}

watch(playSpeed, value => { if (previewVideo.value) previewVideo.value.playbackRate = value })

async function selectAdaptiveFrames() {
  if (!props.taskId || !survey.value || !canSelectAdaptiveFrames.value) return
  selectingFrames.value = true
  error.value = ''
  const remaining = FRAME_LIMIT - props.frameCount
  try {
    // 有本地视频：直接在浏览器里扫描本地文件，不要求上传完整视频。
    if (localVideo.value) {
      videoElement.value?.pause()
      extractingLocal.value = true
      extractionCancel.value = false
      // 有 GPS 走零重叠/转弯算法；无 GPS 退化为固定时间间隔抽帧。
      status.value = hasGpsTelemetry.value
        ? '正在本地扫描视频画面、估算重叠并识别转弯；不会逐帧运行水坑模型…'
        : `正在按固定间隔（每 ${fixedIntervalSeconds.value} 秒）扫描视频画面并过滤模糊帧…`
      const frames = hasGpsTelemetry.value
        ? await extractAdaptiveFramesClient(
            localVideo.value.file,
            survey.value.telemetry,
            {
              maxFrames: remaining,
              scanFps: scanFps.value,
              straightOverlapPercent: straightOverlapPercent.value,
              turnOverlapPercent: turnOverlapPercent.value,
              turnAngleDegrees: turnAngleDegrees.value,
              cameraChangeDegrees: cameraChangeDegrees.value,
              blurFilterEnabled: blurFilterEnabled.value,
              minimumSharpnessPercent: minimumSharpnessPercent.value,
            },
            progress => { extractionProgress.value = progress },
            () => extractionCancel.value,
          )
        : await extractFixedIntervalFramesClient(
            localVideo.value.file,
            {
              maxFrames: remaining,
              intervalSeconds: fixedIntervalSeconds.value,
              blurFilterEnabled: blurFilterEnabled.value,
              minimumSharpnessPercent: minimumSharpnessPercent.value,
            },
            progress => { extractionProgress.value = progress },
            () => extractionCancel.value,
          )
      for (const frame of frames) {
        const saved = await saveMissionTaskFrame(props.taskId, {
          file: frame.file,
          time_seconds: frame.time_seconds,
          latitude: frame.latitude,
          longitude: frame.longitude,
          altitude_m: frame.altitude_m,
          gimbal_pitch_deg: frame.gimbal_pitch_deg,
          selection_reason: frame.selection_reason,
          estimated_overlap_percent: frame.estimated_overlap_percent,
          turn_angle_deg: frame.turn_angle_deg,
          sharpness_ratio_percent: frame.sharpness_ratio_percent,
        })
        emit('frame-saved', { ...saved, file: frame.file })
      }
      const progress = extractionProgress.value
      status.value = frames.length
        ? hasGpsTelemetry.value
          ? `本地自动截帧完成：扫描 ${progress?.scanned ?? frames.length} 个候选时刻，跳过 ${progress?.blurred ?? 0} 张模糊帧，自动选出 ${frames.length} 张并保存到任务。`
          : `固定间隔截帧完成：检查 ${progress?.scanned ?? frames.length} 个时刻，跳过 ${progress?.blurred ?? 0} 张模糊帧，保存 ${frames.length} 张（无 GPS，帧不带坐标）。`
        : hasGpsTelemetry.value
          ? `本地扫描了 ${progress?.scanned ?? 0} 个候选时刻，跳过 ${progress?.blurred ?? 0} 张模糊帧；没有需要新增的关键帧。`
          : `按固定间隔检查了 ${progress?.scanned ?? 0} 个时刻，跳过 ${progress?.blurred ?? 0} 张模糊帧；没有保存关键帧。`
      return
    }
    // 服务器端逐帧分析需要完整视频；本机没有绑定视频时提示重新选择。
    if (!survey.value.has_video) {
      error.value = '浏览器里没有该视频的本地文件，无法自动截帧；请在视频库重新选择并双击该视频后再试。'
      return
    }
    status.value = '正在低分辨率扫描视频画面、估算重叠并识别转弯；不会逐帧运行水坑模型…'
    const result = await selectAdaptiveTaskFrames(props.taskId, survey.value.job_id, {
      straight_overlap_percent: straightOverlapPercent.value,
      turn_overlap_percent: turnOverlapPercent.value,
      scan_fps: scanFps.value,
      turn_angle_degrees: turnAngleDegrees.value,
      camera_change_degrees: cameraChangeDegrees.value,
      blur_filter_enabled: blurFilterEnabled.value,
      minimum_sharpness_percent: minimumSharpnessPercent.value,
    })
    for (const record of result.task_frames) emit('frame-saved', { ...record, file: null })
    status.value = result.task_frames.length
      ? `扫描 ${result.scanned_candidates} 个候选时刻，剔除 ${result.blurred_candidates_skipped} 张模糊帧，自动选出 ${result.task_frames.length} 张（总计 ${result.frame_count_after} 张）。`
        + (result.reached_limit ? '已达到任务关键帧上限。' : '')
      : `扫描了 ${result.scanned_candidates} 个候选时刻，剔除 ${result.blurred_candidates_skipped} 张模糊帧；当前任务没有新增关键帧。`
  } catch (cause) {
    if (cause instanceof ExtractionCancelled) {
      status.value = '已取消本地自动截帧；已保存的关键帧保留。'
      return
    }
    error.value = cause instanceof Error ? cause.message : '自适应视频切帧失败。'
    status.value = ''
  } finally {
    selectingFrames.value = false
    extractingLocal.value = false
    extractionProgress.value = null
  }
}

function onVideoMetadata() {
  const duration = videoElement.value?.duration ?? 0
  videoDuration.value = Number.isFinite(duration) ? duration : 0
  updatePosition(videoElement.value?.currentTime ?? 0)
}

async function selectAdaptiveFramesWithAnalysis() {
  if (!props.taskId || !survey.value || !canQueueAnalyze.value || !localVideo.value) return
  try {
    await ensureQueueModels()
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '无法获取识别模型列表。'
    return
  }
  if (!queueStages.value.length) {
    error.value = '没有可用的识别模型，请检查后端配置。'
    return
  }
  // 快照启动时的任务 id：后续上传与流水线推进都用它，切任务不会串到新任务名下。
  const chainTaskId = props.taskId
  videoElement.value?.pause()
  selectingFrames.value = true
  extractingLocal.value = true
  extractionCancel.value = false
  error.value = ''
  queueSkipped.value = 0
  let uploadChain: Promise<void> = Promise.resolve()
  let jobId = ''
  let enqueuedCount = 0
  try {
    status.value = '正在创建识别队列…'
    const submitted = await submitPhotoAnalysisJob({
      taskId: chainTaskId, frameIds: [], modelId: queueStages.value[0].id, streaming: true,
    })
    jobId = submitted.job_id
    emit('job-started', jobId)
    emit('open-queue')
    // 推进（YOLO done → 提交 GLM 复检）与轮询都在全局单例里，切任务/切模块不断链；
    // stagesCount 传当前可用的级数：GLM 未配置时止步于 YOLO，不对着不可用模型提交。
    startChain(chainTaskId, jobId, 0, queueStages.value.length)
    status.value = hasGpsTelemetry.value
      ? '正在本地扫描视频画面；选出的帧会立即上传并排队识别，无需等全部截帧结束…'
      : `正在按固定间隔（每 ${fixedIntervalSeconds.value} 秒）扫描视频画面；选出的帧会立即上传并排队识别…`
    const uploadSelectedFrame = (frame: ClientSelectedFrame) => {
      // 串行上传链：不阻塞扫描循环，也不会同时发起大量上传。
      uploadChain = uploadChain.then(async () => {
        // 抽帧中途切了任务：立即停止保存，避免帧落进新任务；已入队的照常识别。
        if (props.taskId !== chainTaskId) {
          extractionCancel.value = true
          return
        }
        try {
          const saved = await saveMissionTaskFrame(chainTaskId, {
            file: frame.file,
            time_seconds: frame.time_seconds,
            latitude: frame.latitude,
            longitude: frame.longitude,
            altitude_m: frame.altitude_m,
            gimbal_pitch_deg: frame.gimbal_pitch_deg,
            selection_reason: frame.selection_reason,
            estimated_overlap_percent: frame.estimated_overlap_percent,
            turn_angle_deg: frame.turn_angle_deg,
            sharpness_ratio_percent: frame.sharpness_ratio_percent,
          })
          emit('frame-saved', { ...saved, file: frame.file })
          await appendPhotoAnalysisFrames(jobId, [saved.frame_id])
          enqueuedCount += 1
        } catch {
          // 单帧上传/入队失败只跳过计数，不中断抽帧。
          queueSkipped.value += 1
        }
      })
    }
    if (hasGpsTelemetry.value) {
      await extractAdaptiveFramesClient(
        localVideo.value.file,
        survey.value.telemetry,
        {
          maxFrames: FRAME_LIMIT - props.frameCount,
          scanFps: scanFps.value,
          straightOverlapPercent: straightOverlapPercent.value,
          turnOverlapPercent: turnOverlapPercent.value,
          turnAngleDegrees: turnAngleDegrees.value,
          cameraChangeDegrees: cameraChangeDegrees.value,
          blurFilterEnabled: blurFilterEnabled.value,
          minimumSharpnessPercent: minimumSharpnessPercent.value,
        },
        progress => { extractionProgress.value = progress },
        () => extractionCancel.value,
        uploadSelectedFrame,
      )
    } else {
      await extractFixedIntervalFramesClient(
        localVideo.value.file,
        {
          maxFrames: FRAME_LIMIT - props.frameCount,
          intervalSeconds: fixedIntervalSeconds.value,
          blurFilterEnabled: blurFilterEnabled.value,
          minimumSharpnessPercent: minimumSharpnessPercent.value,
        },
        progress => { extractionProgress.value = progress },
        () => extractionCancel.value,
        uploadSelectedFrame,
      )
    }
  } catch (cause) {
    if (cause instanceof ExtractionCancelled) {
      status.value = '已取消本地自动截帧；已入队的帧会继续识别。'
    } else {
      error.value = cause instanceof Error ? cause.message : '截帧并排队识别失败。'
    }
  } finally {
    try {
      await uploadChain
    } catch {
      // 单帧失败已在链内消化；这里只兜底。
    }
    if (jobId) {
      try {
        await closePhotoAnalysisJob(jobId)
      } catch {
        // 队列可能已自动关闭；继续用全局链轮询拿最终状态。
      }
    }
    selectingFrames.value = false
    extractingLocal.value = false
    extractionProgress.value = null
    if (jobId && !error.value) {
      const base = `截帧完成：入队 ${enqueuedCount} 张${queueSkipped.value ? ` · 上传跳过 ${queueSkipped.value} 张` : ''}。`
      status.value = `${base}识别在后台进行（${queueStatusLabel.value || '统计中'}），可切换任务或模块，进度见“任务视频 / 分析”模块与任务列表徽章；所有截图帧都保存在任务关键帧，不会自动删除。`
    }
  }
}

function seekVideo(event: Event) {
  const value = Number((event.target as HTMLInputElement).value)
  if (videoElement.value) videoElement.value.currentTime = value
  updatePosition(value)
}

async function removeTarget(target: VideoCaptureTarget) {
  if (!survey.value || busyTarget.value) return
  busyTarget.value = true
  error.value = ''
  try {
    const targets = await deleteVideoCaptureTarget(survey.value.job_id, target.id)
    survey.value = { ...survey.value, targets }
    emit('targets-change', targets)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '删除近拍点失败。'
  } finally {
    busyTarget.value = false
  }
}

function seekTarget(target: VideoCaptureTarget) {
  if (videoElement.value) videoElement.value.currentTime = target.time_seconds
  updatePosition(target.time_seconds)
}

function resetSurveyState() {
  // 视频与 SRT 绑定的是本次会话里的本地文件；刷新后不再自动恢复旧轨迹，
  // 回到「未选择文件夹」的初始状态，由用户重新选择导入。
  publishSurvey(null)
  if (localVideo.value) {
    URL.revokeObjectURL(localVideo.value.url)
    localVideo.value = null
  }
}

onMounted(() => {
  resetSurveyState()
  // 预取识别模型列表，让“自动截帧并排队识别”按钮能直接启用。
  void ensureQueueModels().catch(() => {})
  bindPipelineEvents()
  document.addEventListener('keydown', onLibraryKeydown, true)
  window.addEventListener('resize', updateMapRect)
})
onUnmounted(() => {
  offPipelineEvents?.()
  offPipelineEvents = null
  document.removeEventListener('keydown', onLibraryKeydown, true)
  window.removeEventListener('resize', updateMapRect)
  if (previewUrlObject) URL.revokeObjectURL(previewUrlObject)
  if (localVideo.value) URL.revokeObjectURL(localVideo.value.url)
})
watch(() => props.taskId, () => {
  resetSurveyState()
  queueSkipped.value = 0
})
</script>

<template>
  <section class="video-survey-panel" aria-labelledby="video-survey-title">
    <header class="video-survey-header">
      <div>
        <h3 id="video-survey-title">视频轨迹定位</h3>
      </div>
      <span v-if="survey" class="video-survey-badge" :class="survey.has_gps ? 'is-ready' : 'is-warning'">
        {{ survey.has_gps ? `${survey.telemetry_points} 个定位点` : '无 GPS 数据' }}
      </span>
    </header>

    <input ref="folderInput" class="video-survey-input" type="file" webkitdirectory multiple @change="onFolderPicked" />

    <div class="video-survey-picker-row">
      <button type="button" :disabled="loading || !taskId" @click="chooseVideoFolder">选择视频文件夹</button>
      <button v-if="libraryFolderName" type="button" class="video-survey-browse" :disabled="loading" @click="openLibraryBrowser">浏览</button>
      <span class="video-survey-file-name" :title="libraryFolderName">
        {{ !taskId ? '请先创建当前任务' : libraryFolderName || '未选择文件夹' }}
      </span>
    </div>

    <p v-if="error" class="video-survey-feedback is-error" role="alert">{{ error }}</p>

    <div v-if="survey" class="video-survey-review">
      <template v-if="reviewVideoSrc">
        <video
          ref="videoElement"
          class="video-survey-player"
          :src="reviewVideoSrc"
          controls
          playsinline
          preload="metadata"
          @loadedmetadata="onVideoMetadata"
          @timeupdate="updatePosition()"
          @seeked="updatePosition()"
        ></video>
        <div v-if="videoDuration > 0" class="video-survey-timeline">
          <input type="range" min="0" :max="videoDuration" step="0.1" :value="currentTime" aria-label="定位视频时间" @input="seekVideo" />
          <span>{{ currentTimeLabel }} / {{ formatVideoTime(videoDuration) }}</span>
        </div>
      </template>
      <p v-else class="video-survey-note" role="status">
        此任务的视频未上传到服务器（SRT-only 导入）。在上方视频库重新选择并双击该视频即可预览与截帧；
        使用「上传完整视频并自动筛帧」可启用服务器端逐帧分析。
      </p>

      <section class="adaptive-selector" aria-label="自适应筛选视频关键帧">
        <header><strong>自适应筛选关键帧<InfoHint :text="hasGpsTelemetry ? '按画面重叠和 SRT 转弯状态选帧，不使用固定抽帧间隔。' : '当前视频无 GPS 轨迹：按固定时间间隔截帧，帧不带坐标。'" /></strong></header>
        <details class="adaptive-settings-group overview-group">
          <summary><span>抽帧参数设置</span><small>{{ hasGpsTelemetry ? '重叠 / 转弯 / 扫描频率' : `每 ${fixedIntervalSeconds} 秒一帧` }}</small></summary>
          <div class="overview-group-body">
            <div v-if="hasGpsTelemetry" class="adaptive-selector-fields">
              <label>直线段最大重叠 · %<input v-model.number="straightOverlapPercent" type="number" min="0" max="50" step="1" :disabled="selectingFrames" /></label>
              <label>转弯处保留重叠 · %<input v-model.number="turnOverlapPercent" type="number" min="20" max="90" step="1" :disabled="selectingFrames" /></label>
              <label>画面扫描频率<CustomSelect v-model="scanFps" :options="[{ value: 1, label: '1 次 / 秒' }, { value: 2, label: '2 次 / 秒' }, { value: 4, label: '4 次 / 秒' }]" :disabled="selectingFrames" aria-label="画面扫描频率" /></label>
              <label>转弯识别角度 · °<input v-model.number="turnAngleDegrees" type="number" min="10" max="90" step="5" :disabled="selectingFrames" /></label>
              <label>镜头角度变化阈值 · °<input v-model.number="cameraChangeDegrees" type="number" min="5" max="45" step="1" :disabled="selectingFrames" /></label>
              <label class="adaptive-blur-toggle"><input v-model="blurFilterEnabled" type="checkbox" :disabled="selectingFrames" />过滤明显模糊帧</label>
              <label>最低清晰度 · 近期中位数 %<input v-model.number="minimumSharpnessPercent" type="number" min="0" max="100" step="5" :disabled="selectingFrames || !blurFilterEnabled" /></label>
            </div>
            <div v-else class="adaptive-selector-fields adaptive-fields-fixed">
              <label>抽帧间隔<CustomSelect v-model="fixedIntervalSeconds" :options="[{ value: 2, label: '每 2 秒一帧' }, { value: 3, label: '每 3 秒一帧' }, { value: 5, label: '每 5 秒一帧' }]" :disabled="selectingFrames" aria-label="固定抽帧间隔" /></label>
              <label>最低清晰度 · 近期中位数 %<input v-model.number="minimumSharpnessPercent" type="number" min="0" max="100" step="5" :disabled="selectingFrames || !blurFilterEnabled" /></label>
              <label class="adaptive-blur-toggle"><input v-model="blurFilterEnabled" type="checkbox" :disabled="selectingFrames" />过滤明显模糊帧</label>
            </div>
          </div>
        </details>
        <div class="adaptive-selector-fields adaptive-queue-row">
          <small v-if="queueHealthLoaded && !queueStages.length">识别服务未就绪，只能先截帧。</small>
        </div>
        <div class="adaptive-selector-actions-row">
          <button class="adaptive-selector-action" type="button" :disabled="!canSelectAdaptiveFrames" @click="selectAdaptiveFrames">
            {{ adaptiveButtonLabel }}
          </button>
          <button class="adaptive-selector-action is-primary" type="button" :disabled="!canQueueAnalyze" @click="selectAdaptiveFramesWithAnalysis">
            {{ extractingLocal ? '截帧并识别中…' : '自动截帧并排队识别' }}
          </button>
          <button v-if="extractingLocal" type="button" class="adaptive-selector-cancel" @click="extractionCancel = true">取消截帧</button>
        </div>
        <p v-if="queueStatusLabel" class="adaptive-queue-status" role="status">
          {{ queueStatusLabel }}
          <button type="button" class="adaptive-queue-jump" @click="emit('open-queue')">查看识别队列</button>
        </p>
      </section>

      <ul v-if="survey.targets.length" class="video-survey-target-list">
        <li v-for="target in survey.targets" :key="target.id">
          <button class="video-survey-target-summary" type="button" @click="seekTarget(target)">
            <strong>{{ target.label }}</strong>
            <small>{{ formatVideoTime(target.time_seconds) }} · {{ target.latitude !== null && target.longitude !== null ? `${target.latitude.toFixed(6)}, ${target.longitude.toFixed(6)}` : '坐标待补充' }}<template v-if="target.altitude_m !== null"> · {{ target.altitude_m.toFixed(0) }} m</template></small>
          </button>
          <button class="video-survey-remove" type="button" :disabled="busyTarget" :aria-label="`删除${target.label}`" @click="removeTarget(target)">删除</button>
        </li>
      </ul>
    </div>

    <Teleport to="body">
      <div v-if="libraryOpen" class="video-library-page" :style="dockStyle">
        <nav class="video-library-crumbs" aria-label="浏览位置">
          <button type="button" @click="closeLibrary">地图</button>
          <span aria-hidden="true">›</span>
          <button type="button" :disabled="!previewGroup" @click="closePreview">{{ libraryFolderName }}</button>
          <template v-if="previewGroup">
            <span aria-hidden="true">›</span>
            <span class="video-library-crumb-current" :title="previewGroup.video.name">{{ previewGroup.video.name }}</span>
          </template>
          <span class="video-library-spacer"></span>
          <button type="button" class="video-library-close" @click="closeLibrary">关闭</button>
        </nav>

        <div v-if="!previewGroup" class="video-library-body">
          <div class="video-library-main">
            <p v-if="!libraryGroups.length" class="video-library-empty">
              此文件夹没有视频组。视频需与 SRT / LRF 同名成组；单独的 JPG 属于图片，不会显示。
            </p>
            <div v-else class="video-library-grid">
              <button
                v-for="group in libraryGroups"
                :key="group.key"
                type="button"
                class="video-library-card"
                :class="{ 'is-selected': selectedGroup?.key === group.key }"
                @click="selectedGroup = group"
                @dblclick="openPreview(group)"
              >
                <span class="video-library-thumb">
                  <img v-if="thumbnails[group.key]" :src="thumbnails[group.key]" alt="" loading="lazy" />
                  <span v-else class="video-library-thumb-loading" aria-hidden="true">…</span>
                  <span class="video-library-badges">
                    <i :class="group.srt ? 'is-has' : 'is-miss'">{{ group.srt ? 'SRT' : '无 SRT' }}</i>
                    <i v-if="group.lrf" class="is-has">LRF</i>
                  </span>
                </span>
                <strong :title="group.video.name">{{ group.basename }}</strong>
                <small>{{ formatBytes(group.video.size) }} · {{ group.startAt ? formatClock(group.startAt) : '开始未知' }} – {{ formatClock(group.endAt) }}</small>
              </button>
            </div>
          </div>
          <aside v-if="selectedGroup" class="video-library-properties" aria-label="文件属性">
            <strong class="video-library-properties-title" :title="selectedGroup.video.name">{{ selectedGroup.video.name }}</strong>
            <dl>
              <dt>开始录制</dt>
              <dd>{{ selectedGroup.startAt ? formatDateTime(selectedGroup.startAt) : '未知（文件名未含时间）' }}</dd>
              <dt>结束（修改时间）</dt>
              <dd>{{ formatDateTime(selectedGroup.endAt) }}</dd>
              <dt>录制时长</dt>
              <dd>{{ formatDurationBetween(selectedGroup.startAt, selectedGroup.endAt) }}</dd>
              <dt>视频</dt>
              <dd>{{ formatBytes(selectedGroup.video.size) }}</dd>
              <dt>SRT 字幕</dt>
              <dd>
                {{ selectedGroup.srt ? formatBytes(selectedGroup.srt.size) : '无' }}
                <template v-if="selectedGroup.srt">
                  · {{ srtCounts[selectedGroup.key] !== undefined ? `${srtCounts[selectedGroup.key]} 个定位点` : '定位点读取中…' }}
                </template>
              </dd>
              <dt>LRF 代理</dt>
              <dd>{{ selectedGroup.lrf ? formatBytes(selectedGroup.lrf.size) : '无' }}</dd>
            </dl>
            <button type="button" class="video-library-properties-open" @click="openPreview(selectedGroup)">打开预览</button>
          </aside>
        </div>

        <div v-else class="video-library-preview">
          <video
            ref="previewVideo"
            class="video-library-player"
            :src="previewUrl"
            controls
            playsinline
            preload="metadata"
          ></video>
          <div class="video-library-preview-bar">
            <label class="video-library-speed">倍速
              <CustomSelect v-model="playSpeed" :options="[{ value: 0.5, label: '0.5×' }, { value: 1, label: '1×' }, { value: 2, label: '2×' }, { value: 4, label: '4×' }]" aria-label="播放倍速" open-up />
            </label>
            <div class="video-library-preview-meta">
              <span>{{ previewGroup.startAt ? formatDateTime(previewGroup.startAt) : '开始时间未知' }}</span>
              <span>结束 {{ formatDateTime(previewGroup.endAt) }}</span>
              <span>{{ formatDurationBetween(previewGroup.startAt, previewGroup.endAt) }}</span>
              <span v-if="previewGroup.srt">{{ srtCounts[previewGroup.key] !== undefined ? `${srtCounts[previewGroup.key]} 个定位点` : '定位点读取中…' }}</span>
              <span v-else>无 SRT</span>
            </div>
            <div class="video-library-preview-actions">
              <button
                type="button"
                class="video-library-capture"
                :disabled="!canCapturePreview"
                :title="!survey ? '先点击「导入到当前任务」，截帧才能带上 SRT 位置' : props.frameCount >= FRAME_LIMIT ? '关键帧已达上限' : '把当前画面按 SRT 位置存为任务关键帧'"
                @click="capturePreviewFrame"
              >{{ capturingPreview ? '截取中…' : '截取当前画面' }}</button>
              <template v-if="!previewGroup.srt && noSrtImportArmed">
                <span class="video-library-import-warning">没有 SRT：地图不会显示飞行路径，关键帧不带 GPS。确定导入吗？</span>
                <button type="button" class="video-library-import-cancel" :disabled="loading" @click="noSrtImportArmed = false">取消</button>
                <button type="button" class="video-library-import is-armed" :disabled="loading" @click="importPreviewGroup">{{ loading ? '导入中…' : '确认导入' }}</button>
              </template>
              <button v-else type="button" class="video-library-import" :disabled="!canImportPreview" @click="importPreviewGroup">
                {{ loading ? '导入中…' : previewImportBlockedReason || (previewGroup.srt ? '导入到当前任务' : '导入到当前任务（无 SRT）') }}
              </button>
            </div>
          </div>
        </div>
      </div>
    </Teleport>
  </section>
</template>

<style scoped>
.video-survey-panel { margin: 12px 0; padding: 10px; border: 1px solid var(--ui-line); border-radius: var(--ui-radius); background: #fff; font-size: var(--font-caption); }
.video-survey-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; margin-bottom: 9px; }
.video-survey-header h3 { margin: 0; color: #27364a; font-size: 13px; }
.video-survey-header p { margin: 4px 0 0; color: var(--ui-muted); line-height: 1.4; }
.video-survey-badge { flex: 0 0 auto; padding: 3px 7px; border-radius: 10px; font-size: 12px; }
.video-survey-badge.is-ready { background: #e7f6ec; color: #16794c; }
.video-survey-badge.is-warning { background: #fff4e5; color: #9a5b13; }
.video-survey-picker-row { display: flex; min-width: 0; align-items: center; gap: 7px; margin: 5px 0; }
.video-survey-picker-row button, .video-survey-target-list button { min-height: 28px; padding: 4px 8px; border: 1px solid #d7dfe8; border-radius: 6px; background: #fff; color: #34506c; font-size: 12px; white-space: nowrap; }
.video-survey-picker-row button:hover:not(:disabled), .video-survey-target-list button:hover:not(:disabled) { border-color: var(--ui-accent); color: var(--ui-accent); background: #eff6ff; }
.video-survey-picker-row button:disabled, .video-survey-target-list button:disabled { opacity: .5; cursor: not-allowed; }
.video-survey-file-name { min-width: 0; overflow: hidden; color: var(--ui-muted); text-overflow: ellipsis; white-space: nowrap; }
.adaptive-selector { display: grid; gap: 7px; margin-top: 9px; padding: 8px; border: 1px solid var(--ui-line); border-radius: 7px; background: #fbfcfe; }
.adaptive-selector header { display: grid; gap: 3px; }
.adaptive-selector header strong { color: #27364a; font-size: 12px; }
.adaptive-selector header strong { display: inline-flex; align-items: center; }
.adaptive-selector-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px; }
.adaptive-settings-group { border: 1px solid var(--ui-line); border-radius: 7px; background: #fff; }
.adaptive-settings-group > summary { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 7px 9px; cursor: pointer; font-size: 12px; }
.adaptive-settings-group > summary span { min-width: 0; color: #526273; }
.adaptive-settings-group > summary small { margin-left: auto; color: var(--ui-muted); font-size: 12px; }
.adaptive-settings-group .overview-group-body { padding: 2px 9px 9px; }
.adaptive-fields-fixed .adaptive-blur-toggle { grid-column: 1 / -1; justify-content: flex-start; padding: 1px 0; }
.adaptive-selector-fields label { display: grid; min-width: 0; gap: 3px; color: #526273; font-size: 12px; }
.adaptive-selector-fields input, .adaptive-selector-fields select { width: 100%; min-width: 0; min-height: 28px; padding: 3px 5px; border: 1px solid #d7dfe8; border-radius: 5px; background-color: #fff; color: #27364a; font-size: 12px; }
.adaptive-selector-fields select { padding-right: 30px; }
.adaptive-selector-fields .adaptive-blur-toggle { display: flex; align-items: center; gap: 6px; }
.adaptive-selector-fields .adaptive-blur-toggle input { width: 15px; min-height: 15px; margin: 0; accent-color: var(--ui-accent); }
.adaptive-selector-action { width: 100%; border-color: var(--ui-accent) !important; background: var(--ui-accent) !important; color: #fff !important; }
.adaptive-selector-action:hover:not(:disabled) { background: var(--ui-accent-hover) !important; }
.adaptive-selector-action:disabled { opacity: .5; cursor: not-allowed; }
.adaptive-selector-action:focus-visible { outline: 2px solid var(--ui-accent); outline-offset: -2px; box-shadow: none; }
.adaptive-selector-action:not(.is-primary) { border-color: #d7dfe8 !important; background: #fff !important; color: #34506c !important; }
.adaptive-selector-action:not(.is-primary):hover:not(:disabled) { border-color: var(--ui-accent) !important; color: var(--ui-accent) !important; background: #eff6ff !important; }
.adaptive-selector-actions-row { display: flex; gap: 6px; flex-wrap: wrap; }
.adaptive-selector-actions-row .adaptive-selector-action { flex: 1; }
.adaptive-selector-cancel { min-height: 34px; padding: 6px 12px; border: 1px solid #d7dfe8; border-radius: 7px; background: #fff; color: #526273; font-size: 12px; cursor: pointer; }
.adaptive-selector-cancel:hover { border-color: #a4262c; background: #fef2f2; color: #a4262c; }
.adaptive-queue-row { grid-template-columns: minmax(0, 1fr); margin: 0; }
.adaptive-queue-row small { color: var(--ui-muted); font-size: 12px; line-height: 1.4; }
.adaptive-queue-status { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin: 6px 0 0; padding: 6px 8px; border-radius: 6px; background: #f0f7f1; color: #16794c; font-size: 12px; line-height: 1.4; }
.adaptive-queue-jump { min-height: 24px; padding: 2px 8px; border: 1px solid #bcd9c6; border-radius: 6px; background: #fff; color: #16794c; font-size: 12px; cursor: pointer; }
.adaptive-queue-jump:hover { border-color: #16794c; background: #eaf6ee; }
.video-survey-note, .video-survey-feedback { margin: 7px 0; padding: 6px 8px; border-radius: 6px; background: #eff6ff; color: #1d4ed8; line-height: 1.4; overflow-wrap: anywhere; }
.video-survey-feedback.is-error { background: #fef2f2; color: #a4262c; }
.video-survey-review { margin-top: 9px; }
.video-survey-player { display: block; width: 100%; max-height: 270px; border-radius: 7px; background: #111; }
.video-survey-timeline { display: flex; align-items: center; gap: 7px; margin-top: 5px; color: var(--ui-muted); font-size: 12px; }
.video-survey-timeline input { flex: 1; min-width: 0; accent-color: var(--ui-accent); }
.video-survey-timeline span { flex: 0 0 auto; }
.video-survey-review { margin-top: 9px; }
.video-survey-target-list { display: grid; gap: 5px; max-height: 190px; margin: 7px 0 0; padding: 0; overflow-y: auto; list-style: none; }
.video-survey-target-list li { display: flex; min-width: 0; align-items: center; gap: 5px; padding: 5px; border: 1px solid var(--ui-line); border-radius: 6px; background: #fbfcfe; }
.video-survey-target-list .video-survey-target-summary { display: grid; min-width: 0; flex: 1; gap: 2px; border: 0; text-align: left; white-space: normal; }
.video-survey-target-summary strong, .video-survey-target-summary small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.video-survey-target-summary small { color: var(--ui-muted); font-size: 12px; }
.video-survey-target-list .video-survey-remove { flex: 0 0 auto; }
.video-survey-input { display: none; }

.video-library-page { position: fixed; z-index: 6000; display: flex; flex-direction: column; overflow: hidden; border: 1px solid var(--ui-line); border-radius: 10px; background: #f4f7fa; box-shadow: 0 14px 40px rgb(15 23 42 / .24); }
.video-library-crumbs { display: flex; min-width: 0; align-items: center; gap: 8px; padding: 10px 18px; border-bottom: 1px solid var(--ui-line); background: #fff; font-size: 13px; }
.video-library-crumbs > button { min-height: 30px; padding: 4px 14px; border: 1px solid transparent; border-radius: 15px; background: transparent; color: #34506c; font-size: 13px; cursor: pointer; }
.video-library-crumbs > button:hover:not(:disabled) { border-color: #d7dfe8; background: #f3f7fb; }
.video-library-crumbs > button:disabled { opacity: .9; cursor: default; }
.video-library-crumb-current { min-width: 0; overflow: hidden; padding: 4px 14px; border-radius: 15px; background: #eff6ff; color: var(--ui-accent); font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }
.video-library-spacer { flex: 1; }
.video-library-close { min-height: 30px; padding: 4px 14px; border: 1px solid #d7dfe8; border-radius: 6px; background: #fff; color: #34506c; font-size: 12px; cursor: pointer; }
.video-library-close:hover { border-color: var(--ui-accent); background: #eff6ff; color: var(--ui-accent); }
.video-library-body { display: flex; min-height: 0; flex: 1; }
.video-library-main { display: flex; min-width: 0; flex: 1; flex-direction: column; }
.video-library-grid { display: grid; flex: 1; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 14px; align-content: start; overflow-y: auto; padding: 18px 20px; }
.video-library-card { display: grid; gap: 6px; padding: 9px; border: 1px solid var(--ui-line); border-radius: 10px; background: #fff; text-align: left; cursor: pointer; }
.video-library-card:hover { border-color: #b9d4ef; background: #f6fafd; }
.video-library-card.is-selected { border-color: var(--ui-accent); background: #eff6ff; box-shadow: 0 0 0 1px var(--ui-accent); }
.video-library-card strong { overflow: hidden; color: #27364a; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.video-library-card small { color: var(--ui-muted); font-size: 12px; }
.video-library-thumb { position: relative; display: block; aspect-ratio: 16 / 9; overflow: hidden; border-radius: 7px; background: #10151d; }
.video-library-thumb img { display: block; width: 100%; height: 100%; object-fit: cover; }
.video-library-thumb-loading { position: absolute; inset: 0; display: grid; place-items: center; color: #64748b; font-size: 14px; }
.video-library-badges { position: absolute; right: 6px; bottom: 6px; display: flex; gap: 4px; }
.video-library-badges i { padding: 2px 6px; border-radius: 4px; font-size: 12px; font-style: normal; }
.video-library-badges .is-has { background: rgb(22 121 76 / .88); color: #fff; }
.video-library-badges .is-miss { background: rgb(154 91 19 / .9); color: #fff; }
.video-library-empty { margin: 40px auto; color: var(--ui-muted); font-size: 13px; line-height: 1.6; }
.video-library-properties { display: flex; width: 320px; flex: 0 0 auto; flex-direction: column; gap: 10px; overflow-y: auto; padding: 16px 18px; border-left: 1px solid var(--ui-line); background: #fff; }
.video-library-properties-title { overflow: hidden; color: #27364a; font-size: 13px; text-overflow: ellipsis; white-space: nowrap; }
.video-library-properties dl { display: grid; grid-template-columns: auto 1fr; gap: 7px 10px; margin: 0; font-size: 12px; }
.video-library-properties dt { color: var(--ui-muted); white-space: nowrap; }
.video-library-properties dd { margin: 0; color: #27364a; overflow-wrap: anywhere; }
.video-library-properties-open { margin-top: auto; min-height: 34px; border: 1px solid var(--ui-accent); border-radius: 7px; background: var(--ui-accent); color: #fff; font-size: 12px; cursor: pointer; }
.video-library-properties-open:hover { background: var(--ui-accent-hover); }
.video-library-preview { display: flex; min-height: 0; flex: 1; flex-direction: column; }
.video-library-player { display: block; width: 100%; flex: 1; min-height: 0; background: #0b0f16; }
.video-library-preview-bar { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px; padding: 12px 18px; border-top: 1px solid var(--ui-line); background: #fff; }
.video-library-speed { display: flex; align-items: center; gap: 7px; color: var(--ui-muted); font-size: 12px; }
.video-library-preview-meta { display: flex; min-width: 0; flex: 1; flex-wrap: wrap; gap: 4px 14px; color: var(--ui-muted); font-size: 12px; font-variant-numeric: tabular-nums; }
.video-library-preview-actions { display: flex; align-items: center; gap: 10px; }
.video-library-capture { min-height: 34px; padding: 6px 14px; border: 1px solid var(--ui-accent); border-radius: 7px; background: #fff; color: var(--ui-accent); font-size: 12px; cursor: pointer; }
.video-library-capture:hover:not(:disabled) { background: #eff6ff; }
.video-library-capture:disabled { opacity: .55; cursor: not-allowed; }
.video-library-import { min-height: 34px; padding: 6px 16px; border: 1px solid var(--ui-accent); border-radius: 7px; background: var(--ui-accent); color: #fff; font-size: 12px; cursor: pointer; }
.video-library-import:hover:not(:disabled) { background: var(--ui-accent-hover); }
.video-library-import:disabled { opacity: .55; cursor: not-allowed; }
.video-library-import.is-armed { border-color: #c98a8a; background: #fff; color: #963d3d; }
.video-library-import.is-armed:hover:not(:disabled) { background: #fff0f0; }
.video-library-import-cancel { min-height: 34px; padding: 6px 12px; border: 1px solid #d7dfe8; border-radius: 7px; background: #fff; color: #526273; font-size: 12px; cursor: pointer; }
.video-library-import-warning { max-width: 230px; color: #963d3d; font-size: 12px; line-height: 1.45; text-align: right; }
</style>

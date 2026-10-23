<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import CustomSelect from './CustomSelect.vue'
import {
  addVideoCaptureTarget,
  createVideoSurvey,
  deleteVideoCaptureTarget,
  fetchVideoSurvey,
  interpolateVideoPosition,
  selectAdaptiveTaskFrames,
} from './videoSurvey'
import type { VideoCaptureTarget, VideoSurvey, VideoTelemetryPoint } from './videoSurvey'
import type { TaskVideoFrame } from './photoAnalysis'

const props = defineProps<{ taskId: string | null; frameCount: number }>()

const emit = defineEmits<{
  'track-change': [track: number[][]]
  'position-change': [position: VideoTelemetryPoint | null]
  'targets-change': [targets: VideoCaptureTarget[]]
  'frame-capture': [frame: TaskVideoFrame]
  'frames-added': [count: number]
  'survey-change': [survey: VideoSurvey | null]
}>()

const STORAGE_KEY = 'low-altitude-active-video-survey'
const survey = ref<VideoSurvey | null>(null)
const videoInput = ref<HTMLInputElement | null>(null)
const srtInput = ref<HTMLInputElement | null>(null)
const videoFile = ref<File | null>(null)
const srtFile = ref<File | null>(null)
const videoElement = ref<HTMLVideoElement | null>(null)
const currentPosition = ref<VideoTelemetryPoint | null>(null)
const currentTime = ref(0)
const videoDuration = ref(0)
const targetLabel = ref('')
const loading = ref(false)
const busyTarget = ref(false)
const capturingFrame = ref(false)
const selectingFrames = ref(false)
const straightOverlapPercent = ref(12)
const turnOverlapPercent = ref(60)
const scanFps = ref(2)
const turnAngleDegrees = ref(18)
const cameraChangeDegrees = ref(12)
const blurFilterEnabled = ref(true)
const minimumSharpnessPercent = ref(40)
const error = ref('')
const status = ref('')

const canImport = computed(() => Boolean(props.taskId && videoFile.value && srtFile.value && !loading.value))
const canCaptureFrame = computed(() => Boolean(survey.value && currentPosition.value && videoElement.value?.videoWidth
  && props.frameCount < 30 && !capturingFrame.value))
const canSelectAdaptiveFrames = computed(() => Boolean(props.taskId && survey.value?.has_gps
  && props.frameCount < 30 && !selectingFrames.value && !loading.value))
const currentTimeLabel = computed(() => formatVideoTime(currentTime.value))
const videoSelectionName = computed(() => videoFile.value?.name ?? survey.value?.video_name ?? '尚未选择视频')
const srtSelectionName = computed(() => srtFile.value?.name ?? survey.value?.srt_name ?? '尚未选择 SRT')

function formatVideoTime(seconds: number): string {
  const value = Math.max(0, Math.floor(seconds || 0))
  return `${String(Math.floor(value / 60)).padStart(2, '0')}:${String(value % 60).padStart(2, '0')}`
}

function chooseVideoFile() { videoInput.value?.click() }

function chooseSrtFile() { srtInput.value?.click() }

function pickVideoFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (file) {
    videoFile.value = file
    error.value = ''
  }
  input.value = ''
}

function pickSrtFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (file) {
    srtFile.value = file
    error.value = ''
  }
  input.value = ''
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

async function importVideo() {
  if (!props.taskId || !videoFile.value || !srtFile.value || loading.value) return
  loading.value = true
  error.value = ''
  status.value = '正在上传视频并读取 SRT 轨迹…'
  try {
    const result = await createVideoSurvey(videoFile.value, srtFile.value, props.taskId)
    localStorage.setItem(`${STORAGE_KEY}:${props.taskId}`, result.job_id)
    publishSurvey(result)
    status.value = result.has_gps
      ? `已导入 ${result.telemetry_points} 个带位置的 SRT 记录；视频只用于预览和定位，不会逐帧运行模型。`
      : '视频已导入，但 SRT 没有可识别的 GPS 坐标。请确认使用的是与此视频对应、含 GPS 的视频字幕文件。'
    if (videoElement.value) videoElement.value.currentTime = 0
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '视频或 SRT 导入失败。'
    status.value = ''
  } finally {
    loading.value = false
  }
}

async function captureCurrentFrame() {
  const video = videoElement.value
  const point = currentPosition.value
  if (!video || !point || !video.videoWidth || capturingFrame.value) return
  capturingFrame.value = true
  error.value = ''
  try {
    const canvas = document.createElement('canvas')
    const scale = Math.min(1, 1920 / Math.max(video.videoWidth, video.videoHeight))
    canvas.width = Math.max(1, Math.round(video.videoWidth * scale))
    canvas.height = Math.max(1, Math.round(video.videoHeight * scale))
    const context = canvas.getContext('2d')
    if (!context) throw new Error('浏览器无法读取视频画面。')
    context.drawImage(video, 0, 0, canvas.width, canvas.height)
    const blob = await new Promise<Blob>((resolve, reject) => canvas.toBlob(value => value ? resolve(value) : reject(new Error('截取视频帧失败。')), 'image/jpeg', 0.92))
    const totalSeconds = Math.max(0, Math.floor(currentTime.value))
    const name = `frame-${String(Math.floor(totalSeconds / 60)).padStart(2, '0')}m${String(totalSeconds % 60).padStart(2, '0')}s.jpg`
    const file = new File([blob], name, { type: 'image/jpeg' })
    emit('frame-capture', { file, time_seconds: currentTime.value, latitude: point.latitude,
      longitude: point.longitude, altitude_m: point.altitude_m, gimbal_pitch_deg: point.gimbal_pitch_deg })
    status.value = `已把 ${formatVideoTime(currentTime.value)} 的画面加入当前任务分析队列。`
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '截取视频帧失败。'
  } finally {
    capturingFrame.value = false
  }
}

async function selectAdaptiveFrames() {
  if (!props.taskId || !survey.value || !canSelectAdaptiveFrames.value) return
  selectingFrames.value = true
  error.value = ''
  status.value = '正在低分辨率扫描视频画面、估算重叠并识别转弯；不会逐帧运行水坑模型…'
  try {
    const result = await selectAdaptiveTaskFrames(props.taskId, survey.value.job_id, {
      straight_overlap_percent: straightOverlapPercent.value,
      turn_overlap_percent: turnOverlapPercent.value,
      scan_fps: scanFps.value,
      turn_angle_degrees: turnAngleDegrees.value,
      camera_change_degrees: cameraChangeDegrees.value,
      blur_filter_enabled: blurFilterEnabled.value,
      minimum_sharpness_percent: minimumSharpnessPercent.value,
    })
    if (result.task_frames.length) emit('frames-added', result.task_frames.length)
    status.value = result.task_frames.length
      ? `扫描 ${result.scanned_candidates} 个候选时刻，剔除 ${result.blurred_candidates_skipped} 张模糊帧，自动选出 ${result.task_frames.length} 张（总计 ${result.frame_count_after} 张）。`
        + (result.reached_limit ? '已达到任务关键帧上限。' : '')
      : `扫描了 ${result.scanned_candidates} 个候选时刻，剔除 ${result.blurred_candidates_skipped} 张模糊帧；当前任务没有新增关键帧。`
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '自适应视频切帧失败。'
    status.value = ''
  } finally {
    selectingFrames.value = false
  }
}

function onVideoMetadata() {
  const duration = videoElement.value?.duration ?? 0
  videoDuration.value = Number.isFinite(duration) ? duration : 0
  updatePosition(videoElement.value?.currentTime ?? 0)
}

function seekVideo(event: Event) {
  const value = Number((event.target as HTMLInputElement).value)
  if (videoElement.value) videoElement.value.currentTime = value
  updatePosition(value)
}

async function addTarget() {
  if (!survey.value || !currentPosition.value || busyTarget.value) return
  busyTarget.value = true
  error.value = ''
  try {
    const target = await addVideoCaptureTarget(survey.value.job_id, currentTime.value, targetLabel.value)
    survey.value = { ...survey.value, targets: [...survey.value.targets, target] }
    targetLabel.value = ''
    emit('targets-change', survey.value.targets)
    status.value = `已保存 ${target.label}，可按地图坐标前往补拍。`
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '保存近拍点失败。'
  } finally {
    busyTarget.value = false
  }
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

async function restoreTaskSurvey(taskId: string | null) {
  videoFile.value = null
  srtFile.value = null
  publishSurvey(null)
  if (!taskId) return
  const jobId = localStorage.getItem(`${STORAGE_KEY}:${taskId}`)
  if (!jobId) return
  try {
    const restored = await fetchVideoSurvey(jobId)
    if (restored.task_id !== taskId) return
    publishSurvey(restored)
    status.value = '已恢复当前任务的视频轨迹和近拍点。'
  } catch {
    localStorage.removeItem(`${STORAGE_KEY}:${taskId}`)
  }
}

onMounted(() => { void restoreTaskSurvey(props.taskId) })
watch(() => props.taskId, taskId => { void restoreTaskSurvey(taskId) })
</script>

<template>
  <section class="video-survey-panel" aria-labelledby="video-survey-title">
    <header class="video-survey-header">
      <div>
        <h3 id="video-survey-title">视频轨迹定位</h3>
        <p>导入视频和对应的 SRT。播放或拖动进度时，地图显示该时刻的无人机位置。</p>
      </div>
      <span v-if="survey" class="video-survey-badge" :class="survey.has_gps ? 'is-ready' : 'is-warning'">
        {{ survey.has_gps ? `${survey.telemetry_points} 个定位点` : '无 GPS 数据' }}
      </span>
    </header>

    <input ref="videoInput" class="video-survey-input" type="file" accept=".mp4,.mov,.m4v,video/mp4,video/quicktime" @change="pickVideoFile" />
    <input ref="srtInput" class="video-survey-input" type="file" accept=".srt" @change="pickSrtFile" />

    <div class="video-survey-picker-row">
      <button type="button" :disabled="loading || !taskId" @click="chooseVideoFile">选择视频</button>
      <span class="video-survey-file-name" :title="videoFile?.name || survey?.video_name || ''">
        {{ videoSelectionName }}
      </span>
    </div>
    <div class="video-survey-picker-row">
      <button type="button" :disabled="loading || !taskId" @click="chooseSrtFile">选择 SRT</button>
      <span class="video-survey-file-name" :title="srtFile?.name || survey?.srt_name || ''">
        {{ srtSelectionName }}
      </span>
    </div>
    <button class="video-survey-import" type="button" :disabled="!canImport" @click="importVideo">
      {{ !taskId ? '请先创建当前任务' : loading ? '导入中…' : '导入到当前任务' }}
    </button>

    <p v-if="survey" class="video-survey-note" role="status">{{ survey.message }}</p>
    <p
      v-if="survey?.task_match"
      class="video-survey-match"
      :class="`is-${survey.task_match.status.replace('_', '-')}`"
      role="status"
    >
      {{ survey.task_match.message }}
      <template v-if="survey.recording">
        拍摄于 {{ survey.recording.started_at_local }} 至 {{ survey.recording.ended_at_local }}（相机本地时间）。
      </template>
    </p>
    <p v-if="error" class="video-survey-feedback is-error" role="alert">{{ error }}</p>
    <p v-else-if="status" class="video-survey-feedback" role="status">{{ status }}</p>

    <div v-if="survey" class="video-survey-review">
      <video
        ref="videoElement"
        class="video-survey-player"
        :src="survey.video_url"
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

      <div class="video-survey-position">
        <strong>当前画面位置</strong>
        <template v-if="currentPosition">
          <span>{{ currentPosition.latitude.toFixed(6) }}, {{ currentPosition.longitude.toFixed(6) }}</span>
          <small v-if="currentPosition.altitude_m !== null">飞行高度约 {{ currentPosition.altitude_m.toFixed(1) }} m</small>
        </template>
        <small v-else>此时刻没有可用的位置记录</small>
      </div>

      <div class="video-survey-target-entry">
        <input v-model="targetLabel" type="text" maxlength="60" placeholder="近拍点名称（可选）" :disabled="!currentPosition || busyTarget" />
        <button type="button" :disabled="!currentPosition || busyTarget" @click="addTarget">加入近拍清单</button>
      </div>
      <button class="video-survey-capture" type="button" :disabled="!canCaptureFrame" @click="captureCurrentFrame">
        {{ capturingFrame ? '截取中…' : frameCount >= 30 ? '关键帧已达 30 张上限' : '截取当前画面用于分析' }}
      </button>

      <section class="adaptive-selector" aria-label="自适应筛选视频关键帧">
        <header><strong>自适应筛选关键帧</strong><small>按画面重叠和 SRT 转弯状态选帧，不使用固定抽帧间隔。</small></header>
        <div class="adaptive-selector-fields">
          <label>直线段最大重叠 · %<input v-model.number="straightOverlapPercent" type="number" min="0" max="50" step="1" :disabled="selectingFrames" /></label>
          <label>转弯处保留重叠 · %<input v-model.number="turnOverlapPercent" type="number" min="20" max="90" step="1" :disabled="selectingFrames" /></label>
          <label>画面扫描频率<CustomSelect v-model="scanFps" :options="[{ value: 1, label: '1 次 / 秒' }, { value: 2, label: '2 次 / 秒' }, { value: 4, label: '4 次 / 秒' }]" :disabled="selectingFrames" aria-label="画面扫描频率" /></label>
          <label>转弯识别角度 · °<input v-model.number="turnAngleDegrees" type="number" min="10" max="90" step="5" :disabled="selectingFrames" /></label>
          <label>镜头角度变化阈值 · °<input v-model.number="cameraChangeDegrees" type="number" min="5" max="45" step="1" :disabled="selectingFrames" /></label>
          <label class="adaptive-blur-toggle"><input v-model="blurFilterEnabled" type="checkbox" :disabled="selectingFrames" />过滤明显模糊帧</label>
          <label>最低清晰度 · 近期中位数 %<input v-model.number="minimumSharpnessPercent" type="number" min="0" max="100" step="5" :disabled="selectingFrames || !blurFilterEnabled" /></label>
        </div>
        <button class="adaptive-selector-action" type="button" :disabled="!canSelectAdaptiveFrames" @click="selectAdaptiveFrames">
          {{ selectingFrames ? '正在自适应筛帧…' : !survey?.has_gps ? '需要带 GPS 的 SRT' : frameCount >= 30 ? '关键帧已达 30 张上限' : '自动筛选并保存关键帧' }}
        </button>
        <small class="adaptive-selector-note">系统用特征匹配估算相邻画面覆盖，用 SRT 轨迹变化识别转弯；匹配不确定时会保留画面以降低漏区风险。重叠值是筛选目标，不代表地面覆盖的测绘级保证。</small>
      </section>

      <ul v-if="survey.targets.length" class="video-survey-target-list">
        <li v-for="target in survey.targets" :key="target.id">
          <button class="video-survey-target-summary" type="button" @click="seekTarget(target)">
            <strong>{{ target.label }}</strong>
            <small>{{ formatVideoTime(target.time_seconds) }} · {{ target.latitude.toFixed(6) }}, {{ target.longitude.toFixed(6) }}<template v-if="target.altitude_m !== null"> · {{ target.altitude_m.toFixed(0) }} m</template></small>
          </button>
          <button class="video-survey-remove" type="button" :disabled="busyTarget" :aria-label="`删除${target.label}`" @click="removeTarget(target)">删除</button>
        </li>
      </ul>
      <small class="video-survey-footnote">只对手动截取的关键帧运行模型；SRT 依据时间提供无人机位置，不会逐帧调用模型。地图点表示无人机位置，不是水坑地面坐标。</small>
    </div>
  </section>
</template>

<style scoped>
.video-survey-panel { margin: 12px 0; padding: 10px; border: 1px solid var(--ui-line); border-radius: var(--ui-radius); background: #fff; font-size: var(--font-caption); }
.video-survey-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; margin-bottom: 9px; }
.video-survey-header h3 { margin: 0; color: #27364a; font-size: 13px; }
.video-survey-header p { margin: 4px 0 0; color: var(--ui-muted); line-height: 1.4; }
.video-survey-badge { flex: 0 0 auto; padding: 3px 7px; border-radius: 10px; font-size: 10px; }
.video-survey-badge.is-ready { background: #e7f6ec; color: #16794c; }
.video-survey-badge.is-warning { background: #fff4e5; color: #9a5b13; }
.video-survey-picker-row { display: flex; min-width: 0; align-items: center; gap: 7px; margin: 5px 0; }
.video-survey-picker-row button, .video-survey-target-entry button, .video-survey-target-list button, .video-survey-import, .video-survey-capture { min-height: 28px; padding: 4px 8px; border: 1px solid #d7dfe8; border-radius: 6px; background: #fff; color: #34506c; font-size: 11px; white-space: nowrap; }
.video-survey-picker-row button:hover:not(:disabled), .video-survey-target-entry button:hover:not(:disabled), .video-survey-target-list button:hover:not(:disabled) { border-color: var(--ui-accent); color: var(--ui-accent); background: #eff6ff; }
.video-survey-picker-row button:disabled, .video-survey-target-entry button:disabled, .video-survey-target-list button:disabled, .video-survey-import:disabled, .video-survey-capture:disabled { opacity: .5; cursor: not-allowed; }
.video-survey-file-name { min-width: 0; overflow: hidden; color: var(--ui-muted); text-overflow: ellipsis; white-space: nowrap; }
.video-survey-import { width: 100%; margin-top: 4px; border-color: var(--ui-accent); background: var(--ui-accent); color: #fff; }
.video-survey-import:hover:not(:disabled) { background: var(--ui-accent-hover); color: #fff; }
.video-survey-capture { width: 100%; margin-top: 7px; border-color: var(--ui-accent); color: var(--ui-accent); }
.adaptive-selector { display: grid; gap: 7px; margin-top: 9px; padding: 8px; border: 1px solid var(--ui-line); border-radius: 7px; background: #fbfcfe; }
.adaptive-selector header { display: grid; gap: 3px; }
.adaptive-selector header strong { color: #27364a; font-size: 11px; }
.adaptive-selector header small, .adaptive-selector-note { color: var(--ui-muted); font-size: 10px; line-height: 1.4; }
.adaptive-selector-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px; }
.adaptive-selector-fields label { display: grid; min-width: 0; gap: 3px; color: #526273; font-size: 10px; }
.adaptive-selector-fields input, .adaptive-selector-fields select { width: 100%; min-width: 0; min-height: 28px; padding: 3px 5px; border: 1px solid #d7dfe8; border-radius: 5px; background-color: #fff; color: #27364a; font-size: 11px; }
.adaptive-selector-fields select { padding-right: 30px; }
.adaptive-selector-fields .adaptive-blur-toggle { display: flex; align-items: center; gap: 6px; }
.adaptive-selector-fields .adaptive-blur-toggle input { width: 15px; min-height: 15px; margin: 0; accent-color: var(--ui-accent); }
.adaptive-selector-action { width: 100%; border-color: var(--ui-accent) !important; background: var(--ui-accent) !important; color: #fff !important; }
.adaptive-selector-action:hover:not(:disabled) { background: var(--ui-accent-hover) !important; }
.adaptive-selector-action:disabled { opacity: .5; cursor: not-allowed; }
.adaptive-selector-note { margin: 0; }
.video-survey-note, .video-survey-feedback { margin: 7px 0; padding: 6px 8px; border-radius: 6px; background: #eff6ff; color: #1d4ed8; line-height: 1.4; overflow-wrap: anywhere; }
.video-survey-feedback.is-error { background: #fef2f2; color: #a4262c; }
.video-survey-match { margin: 7px 0; padding: 6px 8px; border-radius: 6px; background: #f3f4f6; color: #4b5563; line-height: 1.4; overflow-wrap: anywhere; }
.video-survey-match.is-matched { background: #e7f6ec; color: #16794c; }
.video-survey-match.is-near { background: #fff4e5; color: #9a5b13; }
.video-survey-match.is-off-route { background: #fef2f2; color: #a4262c; }
.video-survey-review { margin-top: 9px; }
.video-survey-player { display: block; width: 100%; max-height: 270px; border-radius: 7px; background: #111; }
.video-survey-timeline { display: flex; align-items: center; gap: 7px; margin-top: 5px; color: var(--ui-muted); font-size: 10px; }
.video-survey-timeline input { flex: 1; min-width: 0; accent-color: var(--ui-accent); }
.video-survey-timeline span { flex: 0 0 auto; }
.video-survey-position { display: grid; gap: 3px; margin-top: 8px; padding: 7px 8px; border: 1px solid var(--ui-line); border-radius: 6px; background: #f9fafb; }
.video-survey-position strong { color: #27364a; }
.video-survey-position span { font-variant-numeric: tabular-nums; }
.video-survey-position small { color: var(--ui-muted); }
.video-survey-target-entry { display: flex; gap: 6px; margin-top: 7px; }
.video-survey-target-entry input { flex: 1; min-width: 0; height: 30px; padding: 4px 7px; border: 1px solid #d1d5db; border-radius: 6px; background: #f9fafb; font-size: var(--font-caption); }
.video-survey-target-list { display: grid; gap: 5px; max-height: 190px; margin: 7px 0 0; padding: 0; overflow-y: auto; list-style: none; }
.video-survey-target-list li { display: flex; min-width: 0; align-items: center; gap: 5px; padding: 5px; border: 1px solid var(--ui-line); border-radius: 6px; background: #fbfcfe; }
.video-survey-target-list .video-survey-target-summary { display: grid; min-width: 0; flex: 1; gap: 2px; border: 0; text-align: left; white-space: normal; }
.video-survey-target-summary strong, .video-survey-target-summary small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.video-survey-target-summary small { color: var(--ui-muted); font-size: 10px; }
.video-survey-target-list .video-survey-remove { flex: 0 0 auto; }
.video-survey-footnote { display: block; margin-top: 8px; color: var(--ui-muted); line-height: 1.4; }
.video-survey-input { display: none; }
</style>

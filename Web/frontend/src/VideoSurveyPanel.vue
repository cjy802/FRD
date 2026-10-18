<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  addVideoCaptureTarget,
  createVideoSurvey,
  deleteVideoCaptureTarget,
  fetchVideoSurvey,
  interpolateVideoPosition,
} from './videoSurvey'
import type { VideoCaptureTarget, VideoSurvey, VideoTelemetryPoint } from './videoSurvey'

const emit = defineEmits<{
  'track-change': [track: number[][]]
  'position-change': [position: VideoTelemetryPoint | null]
  'targets-change': [targets: VideoCaptureTarget[]]
}>()

const STORAGE_KEY = 'low-altitude-active-video-survey'
const survey = ref<VideoSurvey | null>(null)
const videoFile = ref<File | null>(null)
const srtFile = ref<File | null>(null)
const videoInput = ref<HTMLInputElement | null>(null)
const srtInput = ref<HTMLInputElement | null>(null)
const videoElement = ref<HTMLVideoElement | null>(null)
const currentPosition = ref<VideoTelemetryPoint | null>(null)
const currentTime = ref(0)
const videoDuration = ref(0)
const targetLabel = ref('')
const loading = ref(false)
const busyTarget = ref(false)
const error = ref('')
const status = ref('')

const canImport = computed(() => Boolean(videoFile.value && srtFile.value && !loading.value))
const currentTimeLabel = computed(() => formatVideoTime(currentTime.value))

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

function onVideoChanged(event: Event) {
  videoFile.value = (event.target as HTMLInputElement).files?.[0] ?? null
}

function onSrtChanged(event: Event) {
  srtFile.value = (event.target as HTMLInputElement).files?.[0] ?? null
}

function openVideoPicker() { videoInput.value?.click() }
function openSrtPicker() { srtInput.value?.click() }

async function importVideo() {
  if (!videoFile.value || !srtFile.value || loading.value) return
  loading.value = true
  error.value = ''
  status.value = '正在本机保存视频并读取 SRT 轨迹…'
  try {
    const result = await createVideoSurvey(videoFile.value, srtFile.value)
    localStorage.setItem(STORAGE_KEY, result.job_id)
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

onMounted(async () => {
  const jobId = localStorage.getItem(STORAGE_KEY)
  if (!jobId) return
  try {
    const restored = await fetchVideoSurvey(jobId)
    publishSurvey(restored)
    status.value = '已恢复上次导入的视频轨迹和近拍点。'
  } catch {
    localStorage.removeItem(STORAGE_KEY)
  }
})
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

    <div class="video-survey-picker-row">
      <input ref="videoInput" class="video-survey-native-input" type="file" accept="video/mp4,video/quicktime,video/x-m4v,.mp4,.mov,.m4v" @change="onVideoChanged" />
      <button type="button" :disabled="loading" @click="openVideoPicker">选择视频</button>
      <span class="video-survey-file-name" :title="videoFile?.name ?? survey?.video_name ?? ''">
        {{ videoFile?.name ?? survey?.video_name ?? '尚未选择视频' }}
      </span>
    </div>
    <div class="video-survey-picker-row">
      <input ref="srtInput" class="video-survey-native-input" type="file" accept=".srt,application/x-subrip,text/plain" @change="onSrtChanged" />
      <button type="button" :disabled="loading" @click="openSrtPicker">选择 SRT</button>
      <span class="video-survey-file-name" :title="srtFile?.name ?? survey?.srt_name ?? ''">
        {{ srtFile?.name ?? survey?.srt_name ?? '尚未选择 SRT' }}
      </span>
    </div>
    <button class="video-survey-import" type="button" :disabled="!canImport" @click="importVideo">
      {{ loading ? '导入中…' : '导入视频与轨迹' }}
    </button>

    <p v-if="survey" class="video-survey-note" role="status">{{ survey.message }}</p>
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

      <ul v-if="survey.targets.length" class="video-survey-target-list">
        <li v-for="target in survey.targets" :key="target.id">
          <button class="video-survey-target-summary" type="button" @click="seekTarget(target)">
            <strong>{{ target.label }}</strong>
            <small>{{ formatVideoTime(target.time_seconds) }} · {{ target.latitude.toFixed(6) }}, {{ target.longitude.toFixed(6) }}<template v-if="target.altitude_m !== null"> · {{ target.altitude_m.toFixed(0) }} m</template></small>
          </button>
          <button class="video-survey-remove" type="button" :disabled="busyTarget" :aria-label="`删除${target.label}`" @click="removeTarget(target)">删除</button>
        </li>
      </ul>
      <small class="video-survey-footnote">视频只用于回看和定位；模型只处理之后导入的近拍静态照片。地图坐标是无人机位置，不是像素对应的地面水坑坐标。</small>
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
.video-survey-native-input { display: none; }
.video-survey-picker-row button, .video-survey-target-entry button, .video-survey-target-list button, .video-survey-import { min-height: 28px; padding: 4px 8px; border: 1px solid #d7dfe8; border-radius: 6px; background: #fff; color: #34506c; font-size: 11px; white-space: nowrap; }
.video-survey-picker-row button:hover:not(:disabled), .video-survey-target-entry button:hover:not(:disabled), .video-survey-target-list button:hover:not(:disabled) { border-color: var(--ui-accent); color: var(--ui-accent); background: #eff6ff; }
.video-survey-picker-row button:disabled, .video-survey-target-entry button:disabled, .video-survey-target-list button:disabled, .video-survey-import:disabled { opacity: .5; cursor: not-allowed; }
.video-survey-file-name { min-width: 0; overflow: hidden; color: var(--ui-muted); text-overflow: ellipsis; white-space: nowrap; }
.video-survey-import { width: 100%; margin-top: 4px; border-color: var(--ui-accent); background: var(--ui-accent); color: #fff; }
.video-survey-import:hover:not(:disabled) { background: var(--ui-accent-hover); color: #fff; }
.video-survey-note, .video-survey-feedback { margin: 7px 0; padding: 6px 8px; border-radius: 6px; background: #eff6ff; color: #1d4ed8; line-height: 1.4; overflow-wrap: anywhere; }
.video-survey-feedback.is-error { background: #fef2f2; color: #a4262c; }
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
</style>

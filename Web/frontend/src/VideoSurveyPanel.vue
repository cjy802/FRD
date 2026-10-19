<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import {
  addVideoCaptureTarget,
  browseVideoSurveyFiles,
  createVideoSurvey,
  deleteVideoCaptureTarget,
  fetchVideoSurvey,
  interpolateVideoPosition,
  selectAdaptiveTaskFrames,
} from './videoSurvey'
import type { VideoBrowserEntry, VideoBrowserListing, VideoCaptureTarget, VideoSurvey, VideoTelemetryPoint } from './videoSurvey'
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
const videoPath = ref('')
const srtPath = ref('')
const videoElement = ref<HTMLVideoElement | null>(null)
const fileBrowserDialog = ref<HTMLElement | null>(null)
const fileBrowserOpen = ref(false)
const fileBrowserKind = ref<'video' | 'srt'>('video')
const fileBrowser = ref<VideoBrowserListing | null>(null)
const fileBrowserLoading = ref(false)
const fileBrowserError = ref('')
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

const canImport = computed(() => Boolean(props.taskId && videoPath.value && srtPath.value && !loading.value))
const canCaptureFrame = computed(() => Boolean(survey.value && currentPosition.value && videoElement.value?.videoWidth
  && props.frameCount < 30 && !capturingFrame.value))
const canSelectAdaptiveFrames = computed(() => Boolean(props.taskId && survey.value?.has_gps
  && props.frameCount < 30 && !selectingFrames.value && !loading.value))
const currentTimeLabel = computed(() => formatVideoTime(currentTime.value))
const videoSelectionName = computed(() => videoPath.value ? pathBasename(videoPath.value) : survey.value?.video_name ?? '尚未选择视频')
const srtSelectionName = computed(() => srtPath.value ? pathBasename(srtPath.value) : survey.value?.srt_name ?? '尚未选择 SRT')

function formatVideoTime(seconds: number): string {
  const value = Math.max(0, Math.floor(seconds || 0))
  return `${String(Math.floor(value / 60)).padStart(2, '0')}:${String(value % 60).padStart(2, '0')}`
}

function pathBasename(path: string): string {
  return path.split(/[\\/]/).filter(Boolean).at(-1) ?? path
}

function formatFileSize(bytes = 0): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
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

async function openFileBrowser(kind: 'video' | 'srt') {
  fileBrowserKind.value = kind
  fileBrowserOpen.value = true
  fileBrowserError.value = ''
  await loadFileBrowser()
  await nextTick()
  fileBrowserDialog.value?.focus()
}

async function loadFileBrowser(path?: string) {
  fileBrowserLoading.value = true
  fileBrowserError.value = ''
  try {
    fileBrowser.value = await browseVideoSurveyFiles(fileBrowserKind.value, path)
  } catch (cause) {
    fileBrowserError.value = cause instanceof Error ? cause.message : '无法读取这个文件夹。'
  } finally {
    fileBrowserLoading.value = false
  }
}

function selectBrowserEntry(entry: VideoBrowserEntry) {
  if (entry.is_directory) {
    void loadFileBrowser(entry.path)
    return
  }
  if (fileBrowserKind.value === 'video') videoPath.value = entry.path
  else srtPath.value = entry.path
  fileBrowserOpen.value = false
}

function closeFileBrowser() { fileBrowserOpen.value = false }

async function importVideo() {
  if (!props.taskId || !videoPath.value || !srtPath.value || loading.value) return
  loading.value = true
  error.value = ''
  status.value = '正在本机保存视频并读取 SRT 轨迹…'
  try {
    const result = await createVideoSurvey(videoPath.value, srtPath.value, props.taskId)
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
      longitude: point.longitude, altitude_m: point.altitude_m })
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
  videoPath.value = ''
  srtPath.value = ''
  publishSurvey(null)
  if (!taskId) return
  const jobId = localStorage.getItem(`${STORAGE_KEY}:${taskId}`)
  if (!jobId) return
  try {
    const restored = await fetchVideoSurvey(jobId)
    if (restored.task_id !== taskId) return
    publishSurvey(restored)
    videoPath.value = ''
    srtPath.value = ''
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

    <div class="video-survey-picker-row">
      <button type="button" :disabled="loading || !taskId" @click="openFileBrowser('video')">选择视频</button>
      <span class="video-survey-file-name" :title="videoPath || survey?.video_name || ''">
        {{ videoSelectionName }}
      </span>
    </div>
    <div class="video-survey-picker-row">
      <button type="button" :disabled="loading || !taskId" @click="openFileBrowser('srt')">选择 SRT</button>
      <span class="video-survey-file-name" :title="srtPath || survey?.srt_name || ''">
        {{ srtSelectionName }}
      </span>
    </div>
    <button class="video-survey-import" type="button" :disabled="!canImport" @click="importVideo">
      {{ !taskId ? '请先创建当前任务' : loading ? '导入中…' : '导入到当前任务' }}
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
      <button class="video-survey-capture" type="button" :disabled="!canCaptureFrame" @click="captureCurrentFrame">
        {{ capturingFrame ? '截取中…' : frameCount >= 30 ? '关键帧已达 30 张上限' : '截取当前画面用于分析' }}
      </button>

      <section class="adaptive-selector" aria-label="自适应筛选视频关键帧">
        <header><strong>自适应筛选关键帧</strong><small>按画面重叠和 SRT 转弯状态选帧，不使用固定抽帧间隔。</small></header>
        <div class="adaptive-selector-fields">
          <label>直线段最大重叠 · %<input v-model.number="straightOverlapPercent" type="number" min="0" max="50" step="1" :disabled="selectingFrames" /></label>
          <label>转弯处保留重叠 · %<input v-model.number="turnOverlapPercent" type="number" min="20" max="90" step="1" :disabled="selectingFrames" /></label>
          <label>画面扫描频率<select v-model.number="scanFps" :disabled="selectingFrames"><option :value="1">1 次 / 秒</option><option :value="2">2 次 / 秒</option><option :value="4">4 次 / 秒</option></select></label>
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

    <Teleport to="body">
      <div v-if="fileBrowserOpen" class="video-file-browser-backdrop" @click.self="closeFileBrowser">
        <section ref="fileBrowserDialog" class="video-file-browser" role="dialog" aria-modal="true" aria-labelledby="video-file-browser-title" tabindex="-1" @keydown.esc.stop.prevent="closeFileBrowser">
          <header class="video-file-browser-header">
            <div>
              <h4 id="video-file-browser-title">选择{{ fileBrowserKind === 'video' ? '视频' : 'SRT 字幕' }}</h4>
              <small>在网页内浏览本机文件夹</small>
            </div>
            <button type="button" aria-label="关闭文件选择器" @click="closeFileBrowser">关闭</button>
          </header>
          <nav v-if="fileBrowser" class="video-file-browser-roots" aria-label="常用位置和磁盘">
            <button v-for="root in fileBrowser.roots" :key="root.path" type="button" :class="{ 'is-active': root.path === fileBrowser.root.path }" @click="loadFileBrowser(root.path)">{{ root.label }}</button>
          </nav>
          <div class="video-file-browser-path">
            <button v-if="fileBrowser?.parent_path" type="button" @click="loadFileBrowser(fileBrowser.parent_path)">上一级</button>
            <span :title="fileBrowser?.current_path">{{ fileBrowser?.current_path ?? '读取目录…' }}</span>
          </div>
          <p v-if="fileBrowserError" class="video-file-browser-message is-error" role="alert">{{ fileBrowserError }}</p>
          <p v-else-if="fileBrowserLoading" class="video-file-browser-message" role="status">正在读取目录…</p>
          <ul v-else-if="fileBrowser?.entries.length" class="video-file-browser-list">
            <li v-for="entry in fileBrowser.entries" :key="entry.path">
              <button type="button" :class="entry.is_directory ? 'is-directory' : 'is-file'" @click="selectBrowserEntry(entry)">
                <span class="video-file-browser-entry-icon">{{ entry.is_directory ? '文件夹' : fileBrowserKind === 'video' ? '视频' : 'SRT' }}</span>
                <span class="video-file-browser-entry-name">{{ entry.name }}</span>
                <small v-if="!entry.is_directory">{{ formatFileSize(entry.size_bytes) }}</small>
                <span v-else class="video-file-browser-chevron">›</span>
              </button>
            </li>
          </ul>
          <p v-else class="video-file-browser-message">此文件夹里没有{{ fileBrowserKind === 'video' ? '支持的视频' : 'SRT 文件' }}；可以进入子文件夹或切换磁盘。</p>
          <small v-if="fileBrowser?.truncated" class="video-file-browser-truncated">文件较多，只显示前 2000 项，请继续进入子文件夹缩小范围。</small>
        </section>
      </div>
    </Teleport>
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
.video-file-browser-backdrop { position: fixed; z-index: 10000; inset: 0; display: grid; place-items: center; padding: 18px; background: rgb(15 23 42 / .42); }
.video-file-browser { display: flex; width: min(680px, 96vw); max-height: min(760px, 88vh); flex-direction: column; overflow: hidden; border: 1px solid var(--ui-line); border-radius: var(--ui-radius); background: #fff; color: #27364a; box-shadow: 0 12px 36px rgb(15 23 42 / .2); outline: none; }
.video-file-browser-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 12px 14px; border-bottom: 1px solid var(--ui-line); }
.video-file-browser-header h4 { margin: 0; font-size: 14px; }
.video-file-browser-header small { display: block; margin-top: 3px; color: var(--ui-muted); }
.video-file-browser button { min-height: 30px; padding: 5px 9px; border: 1px solid #d7dfe8; border-radius: 6px; background: #fff; color: #34506c; font-size: 12px; cursor: pointer; }
.video-file-browser button:hover:not(:disabled) { border-color: var(--ui-accent); background: #eff6ff; color: var(--ui-accent); }
.video-file-browser-roots { display: flex; flex-wrap: wrap; gap: 6px; padding: 10px 12px 0; }
.video-file-browser-roots button.is-active { border-color: var(--ui-accent); background: #eff6ff; color: var(--ui-accent); }
.video-file-browser-path { display: flex; min-width: 0; align-items: center; gap: 8px; padding: 9px 12px; }
.video-file-browser-path span { overflow: hidden; color: var(--ui-muted); text-overflow: ellipsis; white-space: nowrap; }
.video-file-browser-list { min-height: 120px; margin: 0 12px 12px; padding: 0; overflow: auto; border: 1px solid var(--ui-line); border-radius: 6px; list-style: none; }
.video-file-browser-list li + li { border-top: 1px solid #edf0f3; }
.video-file-browser-list li button { display: flex; width: 100%; align-items: center; gap: 9px; min-height: 40px; padding: 5px 9px; border: 0; border-radius: 0; text-align: left; }
.video-file-browser-list li button:hover { background: #f3f7fb; }
.video-file-browser-entry-icon { flex: 0 0 48px; color: var(--ui-muted); font-size: 10px; }
.video-file-browser-entry-name { min-width: 0; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.video-file-browser-list li button small { flex: 0 0 auto; color: var(--ui-muted); }
.video-file-browser-chevron { color: var(--ui-muted); font-size: 18px; }
.video-file-browser-message { margin: 0 12px 12px; padding: 10px; border-radius: 6px; background: #f3f6f9; color: var(--ui-muted); line-height: 1.4; }
.video-file-browser-message.is-error { background: #fef2f2; color: #a4262c; }
.video-file-browser-truncated { margin: 0 12px 12px; color: var(--ui-muted); }
</style>

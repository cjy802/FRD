<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
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
import {
  countSrtTelemetry,
  formatBytes,
  formatClock,
  formatDateTime,
  formatDurationBetween,
  groupVideoFiles,
  makeVideoThumbnail,
} from './videoLibrary'
import type { VideoLibraryGroup } from './videoLibrary'

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
let previewUrlObject = ''

const canCaptureFrame = computed(() => Boolean(survey.value && currentPosition.value && videoElement.value?.videoWidth
  && props.frameCount < 30 && !capturingFrame.value))
const canSelectAdaptiveFrames = computed(() => Boolean(props.taskId && survey.value?.has_gps
  && props.frameCount < 30 && !selectingFrames.value && !loading.value))
const currentTimeLabel = computed(() => formatVideoTime(currentTime.value))
const previewImportBlockedReason = computed(() => {
  if (!props.taskId) return '请先创建当前任务'
  if (!previewGroup.value?.srt) return '此组没有 SRT，无法导入'
  return ''
})
const canImportPreview = computed(() => Boolean(props.taskId && previewGroup.value?.srt && !loading.value))

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

async function importVideo(video: File, srt: File) {
  if (!props.taskId || loading.value) return
  loading.value = true
  error.value = ''
  status.value = '正在上传视频并读取 SRT 轨迹…'
  try {
    const result = await createVideoSurvey(video, srt, props.taskId)
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

function chooseVideoFolder() { folderInput.value?.click() }

function openLibraryBrowser() { libraryOpen.value = true }

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
  for (const group of groups) {
    // 重新选择文件夹后，旧队列立即失效。
    if (libraryGroups.value !== groups) return
    const thumbnail = await makeVideoThumbnail(group.video)
    if (libraryGroups.value !== groups) return
    if (thumbnail) thumbnails.value = { ...thumbnails.value, [group.key]: thumbnail }
    if (group.srt && srtCounts.value[group.key] === undefined) {
      const count = await countSrtTelemetry(group.srt)
      if (libraryGroups.value !== groups) return
      srtCounts.value = { ...srtCounts.value, [group.key]: count }
    }
  }
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
  if (!group?.srt || !props.taskId || loading.value) return
  await importVideo(group.video, group.srt)
  if (!error.value) closeLibrary()
}

function onLibraryKeydown(event: KeyboardEvent) {
  if (event.key !== 'Escape' || !libraryOpen.value) return
  event.stopPropagation()
  if (previewGroup.value) closePreview()
  else libraryOpen.value = false
}

watch(playSpeed, value => { if (previewVideo.value) previewVideo.value.playbackRate = value })

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

onMounted(() => {
  void restoreTaskSurvey(props.taskId)
  document.addEventListener('keydown', onLibraryKeydown, true)
})
onUnmounted(() => {
  document.removeEventListener('keydown', onLibraryKeydown, true)
  if (previewUrlObject) URL.revokeObjectURL(previewUrlObject)
})
watch(() => props.taskId, taskId => { void restoreTaskSurvey(taskId) })
</script>

<template>
  <section class="video-survey-panel" aria-labelledby="video-survey-title">
    <header class="video-survey-header">
      <div>
        <h3 id="video-survey-title">视频轨迹定位</h3>
        <p>选择视频文件夹：同名 MP4 / SRT / LRF 自动成组，JPG 图片不读取。导入后播放或拖动进度时，地图显示该时刻的无人机位置。</p>
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

    <Teleport to="body">
      <div v-if="libraryOpen" class="video-library-backdrop" @click.self="closeLibrary">
        <section class="video-library" role="dialog" aria-modal="true" aria-label="视频文件夹浏览">
          <header class="video-library-nav">
            <nav class="video-library-crumbs" aria-label="浏览位置">
              <button type="button" @click="closeLibrary">地图</button>
              <span aria-hidden="true">›</span>
              <button type="button" :disabled="!previewGroup" @click="closePreview">{{ libraryFolderName }}</button>
              <template v-if="previewGroup">
                <span aria-hidden="true">›</span>
                <span class="video-library-crumb-current" :title="previewGroup.video.name">{{ previewGroup.video.name }}</span>
              </template>
            </nav>
            <button type="button" class="video-library-close" aria-label="关闭并回到地图" @click="closeLibrary">关闭</button>
          </header>

          <div v-if="!previewGroup" class="video-library-body">
            <div class="video-library-main">
              <p v-if="!libraryGroups.length" class="video-library-empty">
                此文件夹没有视频组。视频需与 SRT / LRF 同名成组；单独的 JPG 属于图片，不会显示。
              </p>
              <div v-else class="video-library-grid" role="listbox" aria-label="视频列表">
                <button
                  v-for="group in libraryGroups"
                  :key="group.key"
                  type="button"
                  role="option"
                  class="video-library-card"
                  :class="{ 'is-selected': selectedGroup?.key === group.key }"
                  :aria-selected="selectedGroup?.key === group.key"
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
              <small class="video-library-hint">单击查看属性 · 双击打开预览 · Esc 逐级返回</small>
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
                <CustomSelect v-model="playSpeed" :options="[{ value: 0.5, label: '0.5×' }, { value: 1, label: '1×' }, { value: 2, label: '2×' }, { value: 4, label: '4×' }]" aria-label="播放倍速" />
              </label>
              <div class="video-library-preview-meta">
                <span>{{ previewGroup.startAt ? formatDateTime(previewGroup.startAt) : '开始时间未知' }}</span>
                <span>结束 {{ formatDateTime(previewGroup.endAt) }}</span>
                <span>{{ formatDurationBetween(previewGroup.startAt, previewGroup.endAt) }}</span>
                <span v-if="previewGroup.srt">{{ srtCounts[previewGroup.key] !== undefined ? `${srtCounts[previewGroup.key]} 个定位点` : '定位点读取中…' }}</span>
                <span v-else>无 SRT</span>
              </div>
              <div class="video-library-preview-actions">
                <p v-if="error" class="video-library-preview-error" role="alert">{{ error }}</p>
                <button type="button" class="video-library-import" :disabled="!canImportPreview" @click="importPreviewGroup">
                  {{ loading ? '导入中…' : previewImportBlockedReason || '导入到当前任务' }}
                </button>
              </div>
            </div>
          </div>
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
.video-survey-picker-row button, .video-survey-target-entry button, .video-survey-target-list button, .video-survey-capture { min-height: 28px; padding: 4px 8px; border: 1px solid #d7dfe8; border-radius: 6px; background: #fff; color: #34506c; font-size: 11px; white-space: nowrap; }
.video-survey-picker-row button:hover:not(:disabled), .video-survey-target-entry button:hover:not(:disabled), .video-survey-target-list button:hover:not(:disabled) { border-color: var(--ui-accent); color: var(--ui-accent); background: #eff6ff; }
.video-survey-picker-row button:disabled, .video-survey-target-entry button:disabled, .video-survey-target-list button:disabled, .video-survey-capture:disabled { opacity: .5; cursor: not-allowed; }
.video-survey-file-name { min-width: 0; overflow: hidden; color: var(--ui-muted); text-overflow: ellipsis; white-space: nowrap; }
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

.video-library-backdrop { position: fixed; z-index: 10000; inset: 0; display: flex; padding: 22px; background: rgb(15 23 42 / .58); }
.video-library { display: flex; width: min(1180px, 96vw); max-height: 90vh; flex: 1; flex-direction: column; overflow: hidden; align-self: center; border: 1px solid var(--ui-line); border-radius: 12px; background: #fff; color: #27364a; box-shadow: 0 18px 48px rgb(15 23 42 / .3); }
.video-library-nav { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 12px 16px; border-bottom: 1px solid var(--ui-line); }
.video-library-crumbs { display: flex; min-width: 0; align-items: center; gap: 8px; font-size: 13px; }
.video-library-crumbs > button { min-height: 28px; padding: 4px 12px; border: 1px solid transparent; border-radius: 14px; background: transparent; color: #34506c; font-size: 13px; cursor: pointer; }
.video-library-crumbs > button:hover:not(:disabled) { border-color: #d7dfe8; background: #f3f7fb; }
.video-library-crumbs > button:disabled { opacity: .9; cursor: default; }
.video-library-crumb-current { min-width: 0; overflow: hidden; padding: 4px 12px; border-radius: 14px; background: #eff6ff; color: var(--ui-accent); font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }
.video-library-close { min-height: 30px; padding: 5px 12px; border: 1px solid #d7dfe8; border-radius: 6px; background: #fff; color: #34506c; font-size: 12px; cursor: pointer; }
.video-library-close:hover { border-color: var(--ui-accent); background: #eff6ff; color: var(--ui-accent); }
.video-library-body { display: flex; min-height: 0; flex: 1; }
.video-library-main { display: flex; min-width: 0; flex: 1; flex-direction: column; }
.video-library-grid { display: grid; flex: 1; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 12px; align-content: start; overflow-y: auto; padding: 14px 16px; }
.video-library-card { display: grid; gap: 5px; padding: 8px; border: 1px solid var(--ui-line); border-radius: 9px; background: #fff; text-align: left; cursor: pointer; }
.video-library-card:hover { border-color: #b9d4ef; background: #f6fafd; }
.video-library-card.is-selected { border-color: var(--ui-accent); background: #eff6ff; box-shadow: 0 0 0 1px var(--ui-accent); }
.video-library-card strong { overflow: hidden; color: #27364a; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.video-library-card small { color: var(--ui-muted); font-size: 10px; }
.video-library-thumb { position: relative; display: block; aspect-ratio: 16 / 9; overflow: hidden; border-radius: 6px; background: #10151d; }
.video-library-thumb img { display: block; width: 100%; height: 100%; object-fit: cover; }
.video-library-thumb-loading { position: absolute; inset: 0; display: grid; place-items: center; color: #64748b; font-size: 14px; }
.video-library-badges { position: absolute; right: 5px; bottom: 5px; display: flex; gap: 4px; }
.video-library-badges i { padding: 2px 6px; border-radius: 4px; font-size: 10px; font-style: normal; }
.video-library-badges .is-has { background: rgb(22 121 76 / .88); color: #fff; }
.video-library-badges .is-miss { background: rgb(154 91 19 / .9); color: #fff; }
.video-library-empty { margin: 30px 16px; color: var(--ui-muted); font-size: 13px; line-height: 1.6; }
.video-library-hint { padding: 8px 16px 12px; border-top: 1px solid var(--ui-line); color: var(--ui-muted); font-size: 11px; }
.video-library-properties { display: flex; width: 290px; flex: 0 0 auto; flex-direction: column; gap: 10px; overflow-y: auto; padding: 14px 16px; border-left: 1px solid var(--ui-line); background: #f9fafb; }
.video-library-properties-title { overflow: hidden; color: #27364a; font-size: 13px; text-overflow: ellipsis; white-space: nowrap; }
.video-library-properties dl { display: grid; grid-template-columns: auto 1fr; gap: 6px 10px; margin: 0; font-size: 12px; }
.video-library-properties dt { color: var(--ui-muted); white-space: nowrap; }
.video-library-properties dd { margin: 0; color: #27364a; overflow-wrap: anywhere; }
.video-library-properties-open { margin-top: auto; min-height: 32px; border: 1px solid var(--ui-accent); border-radius: 7px; background: var(--ui-accent); color: #fff; font-size: 12px; cursor: pointer; }
.video-library-properties-open:hover { background: var(--ui-accent-hover); }
.video-library-preview { display: flex; min-height: 0; flex: 1; flex-direction: column; }
.video-library-player { display: block; width: 100%; flex: 1; min-height: 0; background: #0b0f16; }
.video-library-preview-bar { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px; padding: 12px 16px; border-top: 1px solid var(--ui-line); }
.video-library-speed { display: flex; align-items: center; gap: 7px; color: var(--ui-muted); font-size: 12px; }
.video-library-preview-meta { display: flex; min-width: 0; flex: 1; flex-wrap: wrap; gap: 4px 14px; color: var(--ui-muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.video-library-preview-actions { display: flex; align-items: center; gap: 10px; }
.video-library-preview-error { margin: 0; color: #a4262c; font-size: 11px; max-width: 320px; overflow-wrap: anywhere; }
.video-library-import { min-height: 34px; padding: 6px 16px; border: 1px solid var(--ui-accent); border-radius: 7px; background: var(--ui-accent); color: #fff; font-size: 12px; cursor: pointer; }
.video-library-import:hover:not(:disabled) { background: var(--ui-accent-hover); }
.video-library-import:disabled { opacity: .55; cursor: not-allowed; }
</style>

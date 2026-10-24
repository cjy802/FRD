<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { fetchMissionTask, listMissionTasks, type MissionTask, type MissionTaskSummary } from './missionTasks'
import { fetchPhotoAnalysisJob, photoAnalysisFileUrl, type PhotoAnalysisJob, type PhotoAnalysisResultImage } from './photoAnalysis'

type TargetRecord = {
  id?: string
  label?: string
  latitude?: number | null
  longitude?: number | null
  altitude_m?: number | null
  time_seconds?: number | null
}

type FieldAlert = {
  id: string
  jobId: string
  image: PhotoAnalysisResultImage
  target: TargetRecord
  sourceUrl: string
  overlayUrl: string
  roadLabel: string
  locationLabel: string
  coordinates: string
  areaLabel: string
  confidenceLabel: string
  altitudeLabel: string
}

const summaries = ref<MissionTaskSummary[]>([])
const selectedTaskId = ref('')
const task = ref<MissionTask | null>(null)
const jobs = ref<PhotoAnalysisJob[]>([])
const alerts = ref<FieldAlert[]>([])
const loading = ref(true)
const refreshing = ref(false)
const error = ref('')
const updatedAt = ref('')
const completedIds = ref<string[]>(loadCompletedIds())
const lightbox = ref<FieldAlert | null>(null)
let refreshTimer: ReturnType<typeof setInterval> | null = null

function loadCompletedIds(): string[] {
  try {
    const value = JSON.parse(localStorage.getItem('low-altitude-field-completed-v1') || '[]')
    return Array.isArray(value) ? value.filter(item => typeof item === 'string') : []
  } catch {
    return []
  }
}

function saveCompletedIds() {
  try {
    localStorage.setItem('low-altitude-field-completed-v1', JSON.stringify(completedIds.value.slice(-200)))
  } catch {
    // 本机存储不可用时仍保留当前页面内的状态。
  }
}

function formatDate(value: string | null | undefined): string {
  if (!value) return '刚刚'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '刚刚'
  return new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(date)
}

function formatArea(image: PhotoAnalysisResultImage): string {
  if (typeof image.largest_patch_area_m2 === 'number' && image.largest_patch_area_m2 > 0) {
    return `最大水面约 ${image.largest_patch_area_m2 >= 100 ? Math.round(image.largest_patch_area_m2) : image.largest_patch_area_m2.toFixed(1)} m²`
  }
  if (typeof image.estimated_area_m2 === 'number' && image.estimated_area_m2 > 0) {
    return `估算水面约 ${image.estimated_area_m2 >= 100 ? Math.round(image.estimated_area_m2) : image.estimated_area_m2.toFixed(1)} m²`
  }
  return `疑似区域 ${(image.suspected_fraction * 100).toFixed(1)}%`
}

function formatConfidence(image: PhotoAnalysisResultImage): string {
  return typeof image.mean_confidence_on_suspected === 'number'
    ? `识别置信度 ${(image.mean_confidence_on_suspected * 100).toFixed(0)}%`
    : '模型已标注'
}

function parseLocation(target: TargetRecord): { roadLabel: string; locationLabel: string } {
  const parts = typeof target.label === 'string' ? target.label.split(' · ').map(item => item.trim()).filter(Boolean) : []
  const coordinateIndex = parts.findIndex(item => /^-?\d+(?:\.\d+)?,\s*-?\d+(?:\.\d+)?$/.test(item))
  const roadLabel = coordinateIndex > 1 ? parts.slice(1, coordinateIndex).join(' · ') : ''
  return { roadLabel, locationLabel: roadLabel || '航线附近积水点' }
}

function makeAlert(job: PhotoAnalysisJob, image: PhotoAnalysisResultImage, target: TargetRecord): FieldAlert {
  const latitude = Number(target.latitude)
  const longitude = Number(target.longitude)
  const { roadLabel, locationLabel } = parseLocation(target)
  return {
    id: String(target.id || `analysis-${job.job_id}-${image.name}`),
    jobId: job.job_id,
    image,
    target,
    // 输入原图走 inputs/ 命名空间；分析结果里的 image.name 是内部文件名，
    // 直接请求 basename 会被后端的文件白名单拒绝。
    sourceUrl: photoAnalysisFileUrl(job.job_id, `inputs/${image.name}`),
    overlayUrl: image.overlay ? photoAnalysisFileUrl(job.job_id, image.overlay) : photoAnalysisFileUrl(job.job_id, image.name),
    roadLabel,
    locationLabel,
    coordinates: Number.isFinite(latitude) && Number.isFinite(longitude)
      ? `${latitude.toFixed(6)}, ${longitude.toFixed(6)}`
      : '坐标待补充',
    areaLabel: formatArea(image),
    confidenceLabel: formatConfidence(image),
    altitudeLabel: typeof target.altitude_m === 'number' ? `拍摄高度 ${target.altitude_m.toFixed(0)} m` : '拍摄高度未知',
  }
}

function buildAlerts(taskValue: MissionTask, taskJobs: PhotoAnalysisJob[]): FieldAlert[] {
  const targetRecords = (taskValue.analysis_targets || []) as TargetRecord[]
  const hasSavedTargets = targetRecords.length > 0
  const fallbackJobs = hasSavedTargets
    ? taskJobs
    : (() => {
        // 没有目标快照的旧任务优先使用最新的复检任务；复检任务代表主控端
        // 已经用第二遍模型确认过的结果，现场端不再展示前几轮的全部候选帧。
        const recheckJobs = taskJobs.filter(job => Boolean(job.context?.recheck_of_job_id))
        const candidates = recheckJobs.length ? recheckJobs : taskJobs
        return candidates.slice().sort((first, second) => first.created_at.localeCompare(second.created_at)).slice(-1)
      })()
  const byId = new Map(targetRecords.filter(item => typeof item.id === 'string').map(item => [item.id as string, item]))
  const output: FieldAlert[] = []
  const seen = new Set<string>()
  for (const job of fallbackJobs) {
    const itemMeta = new Map((job.items || []).map(item => [item.name, item.meta]))
    for (const image of job.results?.images || []) {
      const id = `analysis-${job.job_id}-${image.name}`
      const savedTarget = byId.get(id)
      const meta = itemMeta.get(image.name)
      const derivedTarget: TargetRecord = savedTarget || {
        id,
        label: '疑似积水',
        latitude: meta?.latitude,
        longitude: meta?.longitude,
        altitude_m: meta?.altitude_m,
        time_seconds: meta?.time_seconds,
      }
      // 主控端已经把筛选后的目标写进任务时，以它为准，避免把同一批分析里的
      // 普通帧、复检帧全部暴露给现场人员。没有快照的旧任务才从分析结果回退推导。
      if (!savedTarget && hasSavedTargets) continue
      if (!savedTarget && !(Number.isFinite(Number(meta?.latitude)) && Number.isFinite(Number(meta?.longitude)))) continue
      if (!savedTarget && (image.suspected_pixels || 0) <= 0) continue
      if (seen.has(id)) continue
      seen.add(id)
      output.push(makeAlert(job, image, derivedTarget))
    }
  }
  const sorted = output.sort((first, second) => {
    const firstArea = first.image.largest_patch_area_m2 ?? first.image.estimated_area_m2 ?? 0
    const secondArea = second.image.largest_patch_area_m2 ?? second.image.estimated_area_m2 ?? 0
    return secondArea - firstArea
  })
  // 连续航拍帧可能指向同一个积水点。现场端按约 12 米合并，保留面积更大的
  // 一张照片，避免处理人员收到一长串几乎相同的卡片。
  const deduped: FieldAlert[] = []
  for (const candidate of sorted) {
    const latitude = Number(candidate.target.latitude)
    const longitude = Number(candidate.target.longitude)
    const duplicate = deduped.find(existing => {
      const existingLatitude = Number(existing.target.latitude)
      const existingLongitude = Number(existing.target.longitude)
      if (![latitude, longitude, existingLatitude, existingLongitude].every(Number.isFinite)) return false
      const latMetres = (latitude - existingLatitude) * 111_320
      const lonMetres = (longitude - existingLongitude) * 111_320 * Math.cos(latitude * Math.PI / 180)
      return Math.hypot(latMetres, lonMetres) <= 12
    })
    if (!duplicate) deduped.push(candidate)
    else if (candidate.target.label?.includes('双模型确认') && !duplicate.target.label?.includes('双模型确认')) {
      deduped[deduped.indexOf(duplicate)] = candidate
    }
  }
  return deduped
}

const visibleAlerts = computed(() => alerts.value.filter(alert => !completedIds.value.includes(alert.id)))
const completedCount = computed(() => alerts.value.length - visibleAlerts.value.length)
const taskLabel = computed(() => {
  const name = task.value?.name?.trim()
  if (name) return name
  const roads = task.value?.route?.road_names
  if (Array.isArray(roads)) {
    const labels = roads.filter((road): road is string => typeof road === 'string' && road.trim())
    if (labels.length) return labels.slice(0, 2).join(' → ')
  }
  return '最近一次巡查'
})

function navigationUrl(alert: FieldAlert): string | null {
  const latitude = Number(alert.target.latitude)
  const longitude = Number(alert.target.longitude)
  if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) return null
  return `https://uri.amap.com/marker?position=${longitude},${latitude}&name=${encodeURIComponent(alert.locationLabel)}`
}

function markCompleted(alert: FieldAlert) {
  if (!completedIds.value.includes(alert.id)) completedIds.value = [...completedIds.value, alert.id]
  saveCompletedIds()
}

function restoreCompleted() {
  completedIds.value = completedIds.value.filter(id => !id.startsWith('analysis-') || alerts.value.some(alert => alert.id === id))
  saveCompletedIds()
}

async function loadTask(taskId: string) {
  if (!taskId) {
    task.value = null
    jobs.value = []
    alerts.value = []
    return
  }
  const taskValue = await fetchMissionTask(taskId)
  const loadedJobs = (await Promise.all((taskValue.analysis_job_ids || []).map(async jobId => {
    try {
      return await fetchPhotoAnalysisJob(jobId)
    } catch {
      return null
    }
  }))).filter((job): job is PhotoAnalysisJob => job !== null)
  task.value = taskValue
  jobs.value = loadedJobs
  alerts.value = buildAlerts(taskValue, loadedJobs)
  restoreCompleted()
}

async function refresh(initial = false) {
  if (refreshing.value) return
  if (initial) loading.value = true
  else refreshing.value = true
  error.value = ''
  try {
    const taskSummaries = await listMissionTasks()
    summaries.value = taskSummaries
    const queryTaskId = new URLSearchParams(window.location.search).get('task')
    const nextId = queryTaskId && taskSummaries.some(item => item.task_id === queryTaskId)
      ? queryTaskId
      : selectedTaskId.value && taskSummaries.some(item => item.task_id === selectedTaskId.value)
        ? selectedTaskId.value
        : taskSummaries[0]?.task_id || ''
    selectedTaskId.value = nextId
    await loadTask(nextId)
    updatedAt.value = new Date().toISOString()
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '暂时无法读取积水任务。'
  } finally {
    loading.value = false
    refreshing.value = false
  }
}

function onTaskChange(event: Event) {
  const value = (event.target as HTMLSelectElement).value
  selectedTaskId.value = value
  const url = new URL(window.location.href)
  if (value) url.searchParams.set('task', value)
  else url.searchParams.delete('task')
  window.history.replaceState({}, '', url)
  void refresh()
}

function openLightbox(alert: FieldAlert) {
  lightbox.value = alert
}

function closeLightbox() {
  lightbox.value = null
}

watch(lightbox, value => {
  document.body.style.overflow = value ? 'hidden' : ''
})

onMounted(() => {
  document.title = '积水处置 · 现场处理端'
  void refresh(true)
  refreshTimer = window.setInterval(() => void refresh(), 60_000)
})

onBeforeUnmount(() => {
  if (refreshTimer) window.clearInterval(refreshTimer)
  document.body.style.overflow = ''
  document.title = '低空飞行决策工作台'
})
</script>

<template>
  <div class="field-shell">
    <header class="field-topbar">
      <a class="field-brand" href="/" aria-label="返回低空飞行决策工作台">
        <span class="field-brand-mark" aria-hidden="true">水</span>
        <span><strong>积水处置</strong><small>现场处理端</small></span>
      </a>
      <div class="field-top-actions">
        <span class="field-live"><i></i>自动更新</span>
        <button type="button" class="field-refresh-button" :disabled="loading || refreshing" @click="refresh()" aria-label="刷新积水点">{{ refreshing ? '刷新中' : '刷新' }}</button>
      </div>
    </header>

    <main class="field-main">
      <section class="field-hero" aria-labelledby="field-title">
        <div>
          <p class="field-kicker">现场待处理</p>
          <h1 id="field-title">{{ visibleAlerts.length }}<span>处积水</span></h1>
          <p class="field-subtitle">只看位置和现场照片，处理完一处就划掉一处。</p>
        </div>
        <div class="field-hero-meta">
          <span>{{ taskLabel }}</span>
          <small>{{ updatedAt ? `更新于 ${formatDate(updatedAt)}` : '正在同步' }}</small>
        </div>
      </section>

      <section v-if="summaries.length > 1" class="field-task-picker" aria-label="选择巡查任务">
        <label for="field-task">巡查批次</label>
        <select id="field-task" :value="selectedTaskId" @change="onTaskChange">
          <option v-for="item in summaries" :key="item.task_id" :value="item.task_id">{{ item.name || item.route_summary }} · {{ formatDate(item.updated_at) }}</option>
        </select>
      </section>

      <div v-if="loading" class="field-state field-loading" role="status"><span class="field-loader"></span><strong>正在读取最新积水点</strong><small>把位置和现场照片整理好后会显示在这里。</small></div>
      <div v-else-if="error" class="field-state field-error" role="alert"><strong>暂时读不到积水点</strong><small>{{ error }}</small><button type="button" @click="refresh()">重新读取</button></div>
      <div v-else-if="!alerts.length" class="field-state field-empty"><span class="field-empty-mark">✓</span><strong>目前没有可处理的积水点</strong><small>主控端完成航后照片分析后，新的位置会自动出现在这里。</small></div>
      <div v-else-if="!visibleAlerts.length" class="field-state field-empty"><span class="field-empty-mark">✓</span><strong>这一批已全部处理</strong><small>共完成 {{ completedCount }} 处。需要复看时，可刷新主控端任务。</small></div>

      <section v-else class="field-alert-list" aria-label="积水点列表">
        <article v-for="(alert, index) in visibleAlerts" :key="alert.id" class="field-alert-card">
          <header class="field-alert-header">
            <div class="field-alert-index">{{ String(index + 1).padStart(2, '0') }}</div>
            <div class="field-alert-heading">
              <span class="field-alert-badge">疑似积水</span>
              <h2>{{ alert.locationLabel }}</h2>
              <p v-if="alert.roadLabel">{{ alert.roadLabel }}</p>
            </div>
            <span class="field-alert-status">待处理</span>
          </header>

          <div class="field-location-block">
            <div class="field-location-row"><span class="field-pin" aria-hidden="true">⌖</span><div><strong>位置</strong><span>{{ alert.coordinates }}</span></div></div>
            <a v-if="navigationUrl(alert)" class="field-nav-link" :href="navigationUrl(alert) || undefined" target="_blank" rel="noopener noreferrer">打开导航 <span aria-hidden="true">↗</span></a>
          </div>

          <button type="button" class="field-photo-stage" @click="openLightbox(alert)" :aria-label="`查看 ${alert.locationLabel} 的现场照片`">
            <img :src="alert.sourceUrl" :alt="`${alert.locationLabel} 现场照片`" loading="lazy" />
            <span class="field-photo-caption"><span>现场照片</span><span>点击放大</span></span>
          </button>
          <div class="field-photo-note"><span class="field-dot"></span>识别叠图已叠加在照片上，颜色区域是模型标出的疑似积水范围。</div>

          <div class="field-facts" aria-label="积水识别信息">
            <span><b>{{ alert.areaLabel }}</b><small>范围参考</small></span>
            <span><b>{{ alert.confidenceLabel }}</b><small>识别结果</small></span>
            <span><b>{{ alert.altitudeLabel }}</b><small>拍摄信息</small></span>
          </div>
          <button type="button" class="field-complete-button" @click="markCompleted(alert)"><span aria-hidden="true">✓</span>标记为已处理</button>
        </article>
      </section>

      <footer class="field-footer"><span>积水位置来自航后照片分析</span><a href="/">返回主控端</a></footer>
    </main>

    <Teleport to="body">
      <div v-if="lightbox" class="field-lightbox" role="dialog" aria-modal="true" aria-label="查看现场照片" @click.self="closeLightbox">
        <button type="button" class="field-lightbox-close" aria-label="关闭照片" @click="closeLightbox">×</button>
        <div class="field-lightbox-content">
          <img :src="lightbox.overlayUrl" :alt="`${lightbox.locationLabel} 识别叠图`" />
          <div><strong>{{ lightbox.locationLabel }}</strong><span>{{ lightbox.coordinates }} · {{ lightbox.areaLabel }}</span></div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
:global(body) { background: #edf1ec; }
.field-shell { min-height: 100dvh; overflow-y: auto; color: #16231e; background: #edf1ec; font-family: 'Segoe UI Variable', 'Segoe UI', 'Microsoft YaHei UI', sans-serif; }
.field-topbar { position: sticky; top: 0; z-index: 10; display: flex; align-items: center; justify-content: space-between; gap: 16px; min-height: 68px; padding: 12px max(20px, calc((100vw - 720px) / 2 + 20px)); background: #12221c; color: #fff; box-shadow: 0 6px 22px rgb(15 34 26 / .16); }
.field-brand { display: inline-flex; align-items: center; gap: 10px; color: #fff; text-decoration: none; }
.field-brand-mark { display: grid; width: 34px; height: 34px; place-items: center; border-radius: 10px; background: #c7f36b; color: #12221c; font-size: 14px; font-weight: 900; }
.field-brand strong, .field-brand small { display: block; }
.field-brand strong { font-size: 16px; letter-spacing: -.02em; }
.field-brand small { margin-top: 1px; color: #aec0b5; font-size: 11px; }
.field-top-actions { display: flex; align-items: center; gap: 10px; }
.field-live { display: inline-flex; align-items: center; gap: 5px; color: #c8d4cc; font-size: 11px; white-space: nowrap; }
.field-live i { width: 7px; height: 7px; border-radius: 50%; background: #b8ec58; box-shadow: 0 0 0 4px rgb(184 236 88 / .14); }
.field-refresh-button { min-height: 34px; padding: 5px 11px; border: 1px solid #3b5548; border-radius: 8px; background: transparent; color: #fff; font-size: 12px; }
.field-refresh-button:hover:not(:disabled) { border-color: #c7f36b; color: #c7f36b; }
.field-refresh-button:disabled { cursor: wait; opacity: .6; }
.field-main { width: min(100%, 720px); margin: 0 auto; padding: 26px 20px 34px; }
.field-hero { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; padding: 8px 0 24px; border-bottom: 1px solid #d5ded6; }
.field-kicker { margin: 0 0 5px; color: #5d7669; font-size: 12px; font-weight: 700; letter-spacing: .08em; }
.field-hero h1 { margin: 0; color: #16231e; font-size: clamp(42px, 11vw, 72px); font-weight: 750; letter-spacing: -.08em; line-height: .95; }
.field-hero h1 span { margin-left: 8px; color: #5a7065; font-size: 20px; font-weight: 600; letter-spacing: -.03em; }
.field-subtitle { max-width: 250px; margin: 12px 0 0; color: #687a70; font-size: 13px; line-height: 1.5; }
.field-hero-meta { display: grid; flex: 0 0 auto; gap: 4px; max-width: 180px; color: #2f493d; font-size: 12px; text-align: right; }
.field-hero-meta span { overflow: hidden; font-weight: 700; text-overflow: ellipsis; white-space: nowrap; }
.field-hero-meta small { color: #84938b; font-size: 10px; }
.field-task-picker { display: grid; grid-template-columns: auto minmax(0, 1fr); align-items: center; gap: 10px; padding: 14px 0 2px; }
.field-task-picker label { color: #64776c; font-size: 11px; font-weight: 700; white-space: nowrap; }
.field-task-picker select { width: 100%; min-height: 36px; padding: 6px 10px; border: 1px solid #cdd9d0; border-radius: 8px; background: #fff; color: #283d33; font-size: 12px; }
.field-alert-list { display: grid; gap: 18px; padding-top: 22px; }
.field-alert-card { overflow: hidden; border: 1px solid #d6e0d8; border-radius: 14px; background: #fff; box-shadow: 0 8px 24px rgb(42 69 53 / .07); }
.field-alert-header { display: grid; grid-template-columns: 34px minmax(0, 1fr) auto; align-items: start; gap: 11px; padding: 17px 17px 14px; }
.field-alert-index { padding-top: 2px; color: #94a59a; font-size: 12px; font-variant-numeric: tabular-nums; font-weight: 700; }
.field-alert-heading { min-width: 0; }
.field-alert-badge { display: inline-flex; min-height: 22px; align-items: center; padding: 2px 8px; border-radius: 5px; background: #fff1d3; color: #935b05; font-size: 10px; font-weight: 800; letter-spacing: .02em; }
.field-alert-heading h2 { overflow: hidden; margin: 7px 0 0; color: #182920; font-size: 18px; line-height: 1.25; text-overflow: ellipsis; white-space: nowrap; }
.field-alert-heading p { overflow: hidden; margin: 4px 0 0; color: #73847a; font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.field-alert-status { padding-top: 5px; color: #a26a18; font-size: 11px; font-weight: 700; white-space: nowrap; }
.field-location-block { display: flex; align-items: center; justify-content: space-between; gap: 14px; margin: 0 17px; padding: 12px 0; border-top: 1px solid #e5ebe6; border-bottom: 1px solid #e5ebe6; }
.field-location-row { display: flex; align-items: center; min-width: 0; gap: 8px; }
.field-pin { display: grid; width: 28px; height: 28px; place-items: center; border-radius: 8px; background: #edf7d6; color: #54731f; font-size: 19px; font-weight: 700; }
.field-location-row div { display: grid; min-width: 0; gap: 2px; }
.field-location-row strong { color: #334b3d; font-size: 11px; }
.field-location-row span { color: #697a70; font-size: 12px; font-variant-numeric: tabular-nums; }
.field-nav-link { flex: none; color: #517518; font-size: 12px; font-weight: 800; text-decoration: none; }
.field-nav-link:hover { color: #263a0c; text-decoration: underline; }
.field-photo-stage { position: relative; display: block; width: calc(100% - 34px); min-height: 180px; margin: 16px 17px 0; padding: 0; overflow: hidden; border: 0; border-radius: 10px; background: #d7e0da; cursor: zoom-in; }
.field-photo-stage img { display: block; width: 100%; aspect-ratio: 1.65; object-fit: cover; }
.field-photo-caption { position: absolute; right: 10px; bottom: 9px; left: 10px; display: flex; align-items: center; justify-content: space-between; gap: 8px; color: #fff; font-size: 10px; text-shadow: 0 1px 4px rgb(0 0 0 / .7); }
.field-photo-caption span:last-child { opacity: .8; }
.field-photo-note { display: flex; align-items: center; gap: 6px; margin: 9px 17px 0; color: #7a8a80; font-size: 10px; line-height: 1.4; }
.field-dot { width: 6px; height: 6px; flex: none; border-radius: 50%; background: #e29f2e; }
.field-facts { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin: 16px 17px 0; }
.field-facts span { display: grid; min-width: 0; gap: 3px; padding: 9px 10px; border-radius: 8px; background: #f4f7f3; }
.field-facts b { overflow: hidden; color: #30463a; font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.field-facts small { color: #8a9990; font-size: 10px; }
.field-complete-button { display: flex; align-items: center; justify-content: center; gap: 7px; width: calc(100% - 34px); min-height: 43px; margin: 16px 17px 17px; border: 1px solid #bdd68b; border-radius: 8px; background: #f5fbeb; color: #527517; font-size: 13px; font-weight: 800; }
.field-complete-button:hover { border-color: #8eb64e; background: #eaf7ce; }
.field-complete-button span { display: grid; width: 18px; height: 18px; place-items: center; border-radius: 50%; background: #c7f36b; color: #2f4c0d; font-size: 12px; }
.field-state { display: grid; justify-items: center; gap: 9px; padding: 76px 18px 90px; color: #51645a; text-align: center; }
.field-state strong { color: #263b30; font-size: 16px; }
.field-state small { max-width: 310px; color: #7b8b82; font-size: 12px; line-height: 1.55; }
.field-state button { min-height: 36px; margin-top: 5px; padding: 5px 14px; border: 1px solid #b5ca9a; border-radius: 8px; background: #f5fbeb; color: #55751b; font-size: 12px; font-weight: 700; }
.field-loader { width: 28px; height: 28px; border: 3px solid #d2dfd3; border-top-color: #78a03a; border-radius: 50%; animation: field-spin .8s linear infinite; }
@keyframes field-spin { to { transform: rotate(360deg); } }
.field-error strong { color: #954a3f; }
.field-empty-mark { display: grid; width: 44px; height: 44px; place-items: center; border-radius: 50%; background: #dff0ba; color: #55751b; font-size: 22px; font-weight: 800; }
.field-footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 28px 0 8px; color: #94a198; font-size: 10px; }
.field-footer a { color: #607d31; font-weight: 700; text-decoration: none; }
.field-lightbox { position: fixed; inset: 0; z-index: 50; display: grid; place-items: center; padding: 22px; background: rgb(10 22 16 / .88); }
.field-lightbox-close { position: fixed; top: 17px; right: 19px; width: 38px; height: 38px; border: 1px solid #4c6656; border-radius: 50%; background: #162b21; color: #fff; font-size: 26px; line-height: 1; }
.field-lightbox-content { display: grid; width: min(680px, 100%); gap: 12px; color: #fff; }
.field-lightbox-content img { display: block; width: 100%; max-height: 75dvh; border-radius: 10px; object-fit: contain; background: #1b2d24; }
.field-lightbox-content div { display: grid; gap: 3px; }
.field-lightbox-content strong { font-size: 15px; }
.field-lightbox-content span { color: #c6d4ca; font-size: 11px; }
@media (min-width: 640px) {
  .field-main { padding-top: 38px; }
  .field-alert-list { grid-template-columns: repeat(2, minmax(0, 1fr)); align-items: start; }
  .field-alert-card { min-width: 0; }
  .field-alert-header { grid-template-columns: 30px minmax(0, 1fr); }
  .field-alert-status { grid-column: 2; grid-row: 1; justify-self: end; margin-top: 3px; }
}
@media (max-width: 460px) {
  .field-topbar { padding-right: 16px; padding-left: 16px; }
  .field-live { display: none; }
  .field-main { padding-right: 16px; padding-left: 16px; }
  .field-hero { align-items: flex-start; flex-direction: column; }
  .field-hero-meta { width: 100%; text-align: left; }
  .field-facts { grid-template-columns: 1fr; }
}
</style>

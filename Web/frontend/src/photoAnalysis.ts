export type PhotoAnalysisHealth = {
  available: boolean
  busy: boolean
  python: string
  model_dir: string
  max_photos_per_job: number
  message: string
  default_model_id: string
  models: PhotoAnalysisModelOption[]
}

export type PhotoAnalysisModelOption = {
  id: string
  label: string
  description: string
  available: boolean
  message: string
}

export type TaskVideoFrame = {
  frame_id?: string
  file: File
  time_seconds: number
  latitude: number | null
  longitude: number | null
  altitude_m: number | null
  gimbal_pitch_deg?: number | null
  selection_reason?: string | null
  estimated_overlap_percent?: number | null
  turn_angle_deg?: number | null
  sharpness_score?: number | null
  sharpness_ratio_percent?: number | null
  source_frame_index?: number | null
  source_video_time?: number | null
}

export type PhotoAnalysisResultImage = {
  name: string
  original_name?: string
  sha256: string
  width: number
  height: number
  model_input_size: number
  suspected_pixels: number
  suspected_fraction: number
  suspected_patch_count: number
  patch_area_pixels?: number[]
  mean_confidence_on_suspected: number
  instance_count?: number
  altitude_m?: number | null
  gimbal_pitch_deg?: number | null
  camera_fov_deg?: number
  area_estimate?: 'nadir' | 'oblique' | 'none'
  gsd_m?: number | null
  ground_width_m?: number | null
  ground_height_m?: number | null
  estimated_area_m2?: number | null
  largest_patch_area_m2?: number | null
  overlay: string
  suspected_mask: string
  confidence_map: string
}

export type PhotoAnalysisJobItemMeta = {
  time_seconds?: number | null
  latitude?: number | null
  longitude?: number | null
  altitude_m?: number | null
  gimbal_pitch_deg?: number | null
  name?: string | null
}

export type PhotoAnalysisJobItem = {
  frame_id: string
  name: string
  file_name?: string
  meta?: PhotoAnalysisJobItemMeta
  state: 'pending' | 'running' | 'done' | 'failed' | 'cancelled'
  error: string | null
  attempts: number
}

export type PhotoAnalysisJobCounts = {
  total: number
  done: number
  failed: number
  pending: number
  running: number
  cancelled: number
}

export type PhotoAnalysisJob = {
  job_id: string
  state: 'pending' | 'running' | 'done' | 'error' | 'cancelled'
  mode?: 'frames' | 'legacy'
  created_at: string
  finished_at: string | null
  closed?: boolean
  counts?: PhotoAnalysisJobCounts
  items?: PhotoAnalysisJobItem[]
  context: {
    site_name?: string
    note?: string
    frames?: Array<{ time_seconds: number; latitude: number | null; longitude: number | null; altitude_m: number | null; gimbal_pitch_deg?: number | null; [key: string]: unknown }>
  }
  inputs: { name: string; original_name: string; bytes: number }[]
  results: {
    model: {
      default_model: string
      display_name?: string
      engine?: string
      task: string
      water_class: number
      input_size: number
      normalize: string
      confidence_threshold?: number
      model_sha256: string
    }
    images: PhotoAnalysisResultImage[]
    elapsed_seconds: number
    interpretation: string
  } | null
  error: string | null
}

export class PhotoAnalysisApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message)
    this.name = 'PhotoAnalysisApiError'
  }
}

const endpoint = '/api/photo-analysis'

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    const detail = body && typeof body === 'object' && 'detail' in body ? body.detail : null
    throw new PhotoAnalysisApiError(
      typeof detail === 'string' && detail.trim() ? detail : '照片分析接口请求失败，请稍后重试。',
      response.status,
    )
  }
  if (!body || typeof body !== 'object') {
    throw new PhotoAnalysisApiError('照片分析接口返回了无法识别的数据。', response.status)
  }
  return body as T
}

export async function fetchPhotoAnalysisHealth(signal?: AbortSignal): Promise<PhotoAnalysisHealth> {
  const health = await request<PhotoAnalysisHealth>(`${endpoint}/health`, { signal })
  if (typeof health.available !== 'boolean' || typeof health.message !== 'string') {
    throw new PhotoAnalysisApiError('照片分析接口返回了无法识别的数据。', 200)
  }
  return health
}

export type PhotoAnalysisJobSubmitOptions = {
  taskId: string
  frameIds: string[]
  modelId?: string
  siteName?: string
  note?: string
  cameraFovDeg?: number
  streaming?: boolean
}

export async function submitPhotoAnalysisJob(
  options: PhotoAnalysisJobSubmitOptions,
): Promise<{ job_id: string; job_url: string }> {
  return request<{ job_id: string; job_url: string }>(`${endpoint}/jobs`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      task_id: options.taskId,
      frame_ids: options.frameIds,
      model_id: options.modelId ?? '',
      site_name: options.siteName ?? '',
      note: options.note ?? '',
      ...(options.cameraFovDeg !== undefined ? { camera_fov_deg: options.cameraFovDeg } : {}),
      streaming: options.streaming ?? false,
    }),
  })
}

export async function appendPhotoAnalysisFrames(jobId: string, frameIds: string[]): Promise<number> {
  const payload = await request<{ job_id: string; appended: number }>(
    `${endpoint}/jobs/${encodeURIComponent(jobId)}/frames`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ frame_ids: frameIds }),
    })
  return payload.appended
}

export async function closePhotoAnalysisJob(jobId: string): Promise<void> {
  await request<{ closed: boolean }>(`${endpoint}/jobs/${encodeURIComponent(jobId)}/close`, { method: 'POST' })
}

export async function cancelPhotoAnalysisJob(jobId: string): Promise<void> {
  await request<{ cancelled: boolean }>(`${endpoint}/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' })
}

export async function retryFailedPhotoAnalysisFrames(jobId: string): Promise<number> {
  const payload = await request<{ retried: number }>(
    `${endpoint}/jobs/${encodeURIComponent(jobId)}/retry-failed`, { method: 'POST' })
  return payload.retried
}

export function fetchPhotoAnalysisJob(jobId: string, signal?: AbortSignal): Promise<PhotoAnalysisJob> {
  return request<PhotoAnalysisJob>(`${endpoint}/jobs/${encodeURIComponent(jobId)}`, { signal })
}

export function photoAnalysisFileUrl(jobId: string, fileName: string): string {
  return `${endpoint}/jobs/${encodeURIComponent(jobId)}/files/${encodeURIComponent(fileName)}`
}

export type WaterAlertLevel = 'none' | 'attention' | 'elevated'

export type WaterAlertThresholds = {
  minAlertAreaM2: number
  cameraFovDeg: number
}

export const DEFAULT_WATER_ALERT_THRESHOLDS: WaterAlertThresholds = { minAlertAreaM2: 10, cameraFovDeg: 75 }
export const WATER_ALERT_THRESHOLDS_STORAGE_KEY = 'low-altitude-water-alert-thresholds-v1'

export function sanitizeWaterAlertThresholds(value: Partial<WaterAlertThresholds> | null | undefined): WaterAlertThresholds {
  const area = Number(value?.minAlertAreaM2)
  const fov = Number(value?.cameraFovDeg)
  return {
    minAlertAreaM2: Number.isFinite(area) && area > 0 ? Math.min(area, 100000) : DEFAULT_WATER_ALERT_THRESHOLDS.minAlertAreaM2,
    cameraFovDeg: Number.isFinite(fov) && fov >= 30 && fov <= 120 ? fov : DEFAULT_WATER_ALERT_THRESHOLDS.cameraFovDeg,
  }
}

export function loadWaterAlertThresholds(): WaterAlertThresholds {
  try {
    const raw = localStorage.getItem(WATER_ALERT_THRESHOLDS_STORAGE_KEY)
    return raw ? sanitizeWaterAlertThresholds(JSON.parse(raw)) : { ...DEFAULT_WATER_ALERT_THRESHOLDS }
  } catch {
    return { ...DEFAULT_WATER_ALERT_THRESHOLDS }
  }
}

export function saveWaterAlertThresholds(thresholds: WaterAlertThresholds): void {
  try {
    localStorage.setItem(WATER_ALERT_THRESHOLDS_STORAGE_KEY, JSON.stringify(sanitizeWaterAlertThresholds(thresholds)))
  } catch {
    // 存储不可用时仅保留当前会话内的值。
  }
}

export type WaterAlertAssessment = {
  level: WaterAlertLevel
  label: string
  reason: string
  areaText: string | null
}

function formatArea(value: number): string {
  return value >= 100 ? `${Math.round(value)} m²` : `${value.toFixed(1)} m²`
}

export function assessWaterAlert(image: PhotoAnalysisResultImage, thresholds: WaterAlertThresholds): WaterAlertAssessment {
  const total = image.estimated_area_m2 ?? null
  const largest = image.largest_patch_area_m2 ?? null
  const hasWater = (image.suspected_pixels ?? 0) > 0
  if (!hasWater) {
    return { level: 'none', label: '未见积水', reason: '模型未检出疑似积水。', areaText: null }
  }
  if (total === null) {
    return { level: 'none', label: '高度未知', reason: '该帧没有有效拍摄高度，未做面积估算，不参与警报。', areaText: null }
  }
  const areaText = `约 ${formatArea(total)}${largest !== null && largest > 0 ? ` · 最大斑块 ${formatArea(largest)}` : ''}`
  const lowPrecision = image.area_estimate === 'oblique'
  if (largest !== null && largest >= thresholds.minAlertAreaM2 && !lowPrecision) {
    return {
      level: 'elevated',
      label: '积水警报',
      reason: `最大单块水面约 ${formatArea(largest)}，超过 ${thresholds.minAlertAreaM2} m² 警报阈值。`,
      areaText,
    }
  }
  if (total >= thresholds.minAlertAreaM2) {
    return {
      level: 'attention',
      label: lowPrecision ? '积水注意 · 精度低' : '积水注意',
      reason: lowPrecision
        ? `估算约 ${formatArea(total)}，但该帧为斜拍（云台俯角不足），面积精度低，仅作注意。`
        : `总估算约 ${formatArea(total)}，但最大斑块仅约 ${formatArea(largest ?? 0)}，呈破碎小水坑，未达警报阈值。`,
      areaText,
    }
  }
  return {
    level: 'none',
    label: '小范围积水',
    reason: `估算约 ${formatArea(total)}，低于 ${thresholds.minAlertAreaM2} m² 阈值，不警报。`,
    areaText,
  }
}

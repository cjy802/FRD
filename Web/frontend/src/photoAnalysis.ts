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
  latitude: number
  longitude: number
  altitude_m: number | null
  selection_reason?: string | null
  estimated_overlap_percent?: number | null
  turn_angle_deg?: number | null
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
  mean_confidence_on_suspected: number
  instance_count?: number
  overlay: string
  suspected_mask: string
  confidence_map: string
}

export type PhotoAnalysisJob = {
  job_id: string
  state: 'pending' | 'running' | 'done' | 'error'
  created_at: string
  finished_at: string | null
  context: {
    site_name?: string
    note?: string
    frames?: Array<{ time_seconds: number; latitude: number; longitude: number; altitude_m: number | null; [key: string]: unknown }>
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

export async function submitPhotoAnalysis(
  files: File[],
  context: { site_name?: string; note?: string } = {},
  modelId = '',
  taskId = '',
  frameMetadata: Omit<TaskVideoFrame, 'file'>[] = [],
): Promise<{ job_id: string; job_url: string }> {
  const form = new FormData()
  for (const file of files) form.append('photos', file, file.name)
  if (context.site_name) form.append('site_name', context.site_name)
  if (context.note) form.append('note', context.note)
  if (modelId) form.append('model_id', modelId)
  if (taskId) form.append('task_id', taskId)
  form.append('frame_metadata', JSON.stringify(frameMetadata))
  return request<{ job_id: string; job_url: string }>(`${endpoint}/jobs`, { method: 'POST', body: form })
}

export function fetchPhotoAnalysisJob(jobId: string, signal?: AbortSignal): Promise<PhotoAnalysisJob> {
  return request<PhotoAnalysisJob>(`${endpoint}/jobs/${encodeURIComponent(jobId)}`, { signal })
}

export function photoAnalysisFileUrl(jobId: string, fileName: string): string {
  return `${endpoint}/jobs/${encodeURIComponent(jobId)}/files/${encodeURIComponent(fileName)}`
}

export type VideoTelemetryPoint = {
  time_seconds: number
  latitude: number
  longitude: number
  altitude_m: number | null
  heading_deg: number | null
  gimbal_pitch_deg: number | null
  interpolation_gap_seconds?: number
}

export type VideoCaptureTarget = VideoTelemetryPoint & {
  id: string
  label: string
  created_at: string
}

export type VideoSurvey = {
  job_id: string
  task_id?: string | null
  created_at: string
  video_name: string
  srt_name: string
  video_file: string
  video_bytes: number
  telemetry_points: number
  has_gps: boolean
  message: string
  telemetry: VideoTelemetryPoint[]
  targets: VideoCaptureTarget[]
  video_url: string
}

export type VideoBrowserRoot = { label: string; path: string }
export type VideoBrowserEntry = { name: string; path: string; is_directory: boolean; size_bytes?: number }
export type VideoBrowserListing = {
  kind: 'video' | 'srt'
  roots: VideoBrowserRoot[]
  root: VideoBrowserRoot
  current_path: string
  parent_path: string | null
  entries: VideoBrowserEntry[]
  truncated: boolean
}

export class VideoSurveyApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message)
    this.name = 'VideoSurveyApiError'
  }
}

const endpoint = '/api/video-surveys'

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    const detail = body && typeof body === 'object' && 'detail' in body ? body.detail : null
    throw new VideoSurveyApiError(
      typeof detail === 'string' && detail.trim() ? detail : '视频定位接口请求失败，请稍后重试。',
      response.status,
    )
  }
  if (!body || typeof body !== 'object') {
    throw new VideoSurveyApiError('视频定位接口返回了无法识别的数据。', response.status)
  }
  return body as T
}

export function browseVideoSurveyFiles(kind: 'video' | 'srt', path?: string): Promise<VideoBrowserListing> {
  const query = new URLSearchParams({ kind })
  if (path) query.set('path', path)
  return request<VideoBrowserListing>(`${endpoint}/browse?${query.toString()}`)
}

export async function createVideoSurvey(videoPath: string, srtPath: string, taskId: string): Promise<VideoSurvey> {
  return request<VideoSurvey>(`${endpoint}/import`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ video_path: videoPath, srt_path: srtPath, task_id: taskId }),
  })
}

export function fetchVideoSurvey(jobId: string): Promise<VideoSurvey> {
  return request<VideoSurvey>(`${endpoint}/${encodeURIComponent(jobId)}`)
}

export function videoSurveyUrl(jobId: string): string {
  return `${endpoint}/${encodeURIComponent(jobId)}/video`
}

export function interpolateVideoPosition(points: VideoTelemetryPoint[], timeSeconds: number): VideoTelemetryPoint | null {
  if (!points.length || !Number.isFinite(timeSeconds)) return null
  if (timeSeconds <= points[0].time_seconds) {
    return points[0].time_seconds - timeSeconds <= 2 ? { ...points[0], time_seconds: timeSeconds } : null
  }
  const last = points[points.length - 1]
  if (timeSeconds >= last.time_seconds) {
    return timeSeconds - last.time_seconds <= 2 ? { ...last, time_seconds: timeSeconds } : null
  }
  let low = 0
  let high = points.length - 1
  while (low + 1 < high) {
    const middle = Math.floor((low + high) / 2)
    if (points[middle].time_seconds <= timeSeconds) low = middle
    else high = middle
  }
  const before = points[low]
  const after = points[high]
  const gap = after.time_seconds - before.time_seconds
  const ratio = gap > 0 ? (timeSeconds - before.time_seconds) / gap : 0
  const blend = (a: number | null, b: number | null): number | null => a !== null && b !== null ? a + (b - a) * ratio : a ?? b
  let heading = blend(before.heading_deg, after.heading_deg)
  if (before.heading_deg !== null && after.heading_deg !== null) {
    const delta = (after.heading_deg - before.heading_deg + 540) % 360 - 180
    heading = (before.heading_deg + delta * ratio + 360) % 360
  }
  return {
    time_seconds: timeSeconds,
    latitude: before.latitude + (after.latitude - before.latitude) * ratio,
    longitude: before.longitude + (after.longitude - before.longitude) * ratio,
    altitude_m: blend(before.altitude_m, after.altitude_m),
    heading_deg: heading,
    gimbal_pitch_deg: blend(before.gimbal_pitch_deg, after.gimbal_pitch_deg),
    interpolation_gap_seconds: gap,
  }
}

export function addVideoCaptureTarget(jobId: string, timeSeconds: number, label = ''): Promise<VideoCaptureTarget> {
  return request<VideoCaptureTarget>(`${endpoint}/${encodeURIComponent(jobId)}/targets`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ time_seconds: timeSeconds, label }),
  })
}

export async function deleteVideoCaptureTarget(jobId: string, targetId: string): Promise<VideoCaptureTarget[]> {
  const response = await request<{ targets: VideoCaptureTarget[] }>(
    `${endpoint}/${encodeURIComponent(jobId)}/targets/${encodeURIComponent(targetId)}`,
    { method: 'DELETE' },
  )
  return response.targets
}

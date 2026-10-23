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

export type VideoRecordingWindow = {
  started_at_local: string
  ended_at_local: string
}

export type VideoTaskMatch = {
  status: 'matched' | 'near' | 'off_route' | 'no_gps' | 'no_route'
  median_deviation_m?: number
  p90_deviation_m?: number
  max_deviation_m?: number
  within_30m_percent?: number
  sample_points?: number
  message: string
}

export type VideoSurvey = {
  job_id: string
  task_id?: string | null
  created_at: string
  video_name: string
  srt_name: string
  video_file: string | null
  video_bytes: number
  has_video?: boolean
  telemetry_points: number
  has_gps: boolean
  recording?: VideoRecordingWindow | null
  task_match?: VideoTaskMatch | null
  message: string
  telemetry: VideoTelemetryPoint[]
  targets: VideoCaptureTarget[]
  video_url: string | null
}

export type AdaptiveFrameSelectionOptions = {
  straight_overlap_percent: number
  turn_overlap_percent: number
  scan_fps: number
  turn_angle_degrees: number
  camera_change_degrees: number
  blur_filter_enabled: boolean
  minimum_sharpness_percent: number
}

export type AdaptiveFrameSelectionResult = {
  video_fps: number
  video_frames: number
  duration_seconds: number
  scan_fps: number
  blur_filter_enabled: boolean
  minimum_sharpness_percent: number
  blurred_candidates_skipped: number
  scanned_candidates: number
  reached_limit: boolean
  elapsed_seconds: number
  strategy: string
  frame_count_before: number
  frame_count_after: number
  task_frames: Array<{
    frame_id: string
    name: string
    time_seconds: number
    latitude: number
    longitude: number
    altitude_m: number | null
    selection_reason?: string
    estimated_overlap_percent?: number | null
    turn_angle_deg?: number | null
    sharpness_score?: number | null
    sharpness_ratio_percent?: number | null
  }>
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

// 只上传 SRT：视频保留在用户本机，预览与截帧直接读本地文件。
export function createVideoSurveySrt(srt: File, videoName: string, taskId: string): Promise<VideoSurvey> {
  const form = new FormData()
  form.append('srt', srt)
  form.append('video_name', videoName)
  form.append('task_id', taskId)
  return request<VideoSurvey>(endpoint, { method: 'POST', body: form })
}

export function selectAdaptiveTaskFrames(taskId: string, surveyId: string,
  options: AdaptiveFrameSelectionOptions): Promise<AdaptiveFrameSelectionResult> {
  return request<AdaptiveFrameSelectionResult>(`/api/tasks/${encodeURIComponent(taskId)}/adaptive-frames`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ survey_id: surveyId, ...options }),
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

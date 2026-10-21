import type { TaskVideoFrame } from './photoAnalysis'

export type MissionTask = {
  task_id: string
  name?: string | null
  created_at: string
  updated_at: string
  status: 'planning' | 'ready' | 'in_flight' | 'completed'
  route: Record<string, unknown>
  departure_time: string
  departure_auto_managed?: boolean
  weather: Record<string, unknown> | null
  aircraft: Record<string, unknown> | null
  export_file_name: string | null
  flight_started_at: string | null
  video_survey_id: string | null
  analysis_job_ids: string[]
  analysis_targets: Record<string, unknown>[]
}

export type MissionTaskSummary = {
  task_id: string
  name: string
  created_at: string
  updated_at: string
  status: MissionTask['status']
  route_summary: string
  distance_km: number | null
  departure_time: string | null
  weather_level: string | null
  max_wind_m_s: number | null
  max_rain_6h_mm: number | null
  drone_model: string | null
  video_survey_id: string | null
  frame_count: number
}

export class MissionTaskError extends Error {
  constructor(message: string, readonly status: number) {
    super(message)
    this.name = 'MissionTaskError'
  }
}

export type StoredMissionFrame = Omit<TaskVideoFrame, 'file'> & {
  frame_id: string
  name: string
  file_name: string
  bytes: number
  created_at: string
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  const payload: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    const detail = payload && typeof payload === 'object' && 'detail' in payload ? payload.detail : null
    throw new MissionTaskError(typeof detail === 'string' ? detail : '任务记录请求失败。', response.status)
  }
  return payload as T
}

export function createMissionTask(route: Record<string, unknown>, departureTime: string, departureAutoManaged = true,
  details: { name?: string; weather?: Record<string, unknown> | null; aircraft?: Record<string, unknown> | null } = {}): Promise<MissionTask> {
  return request<MissionTask>('/api/tasks', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ route, departure_time: departureTime, departure_auto_managed: departureAutoManaged, ...details }),
  })
}

export function fetchMissionTask(taskId: string): Promise<MissionTask> {
  return request<MissionTask>(`/api/tasks/${encodeURIComponent(taskId)}`)
}

export async function listMissionTasks(): Promise<MissionTaskSummary[]> {
  const response = await request<{ tasks: MissionTaskSummary[] }>('/api/tasks')
  return response.tasks
}

export async function deleteMissionTask(taskId: string): Promise<void> {
  const response = await fetch(`/api/tasks/${encodeURIComponent(taskId)}`, { method: 'DELETE' })
  if (!response.ok) {
    const payload: unknown = await response.json().catch(() => null)
    const detail = payload && typeof payload === 'object' && 'detail' in payload ? payload.detail : null
    throw new MissionTaskError(typeof detail === 'string' ? detail : '无法删除这个任务。', response.status)
  }
}

export function updateMissionTask(taskId: string, changes: Partial<Pick<MissionTask,
  'status' | 'weather' | 'aircraft' | 'export_file_name' | 'flight_started_at' | 'video_survey_id' | 'analysis_targets' | 'departure_time' | 'departure_auto_managed'>> & { analysis_job_id?: string }): Promise<MissionTask> {
  return request<MissionTask>(`/api/tasks/${encodeURIComponent(taskId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(changes),
  })
}
export async function saveMissionTaskFrame(taskId: string, frame: TaskVideoFrame): Promise<StoredMissionFrame> {
  const form = new FormData()
  form.append('frame', frame.file, frame.file.name)
  form.append('time_seconds', String(frame.time_seconds))
  if (frame.latitude !== null) form.append('latitude', String(frame.latitude))
  if (frame.longitude !== null) form.append('longitude', String(frame.longitude))
  if (frame.altitude_m !== null) form.append('altitude_m', String(frame.altitude_m))
  if (frame.gimbal_pitch_deg !== null && frame.gimbal_pitch_deg !== undefined) form.append('gimbal_pitch_deg', String(frame.gimbal_pitch_deg))
  if (frame.selection_reason) form.append('selection_reason', frame.selection_reason)
  return request<StoredMissionFrame>(`/api/tasks/${encodeURIComponent(taskId)}/frames`, { method: 'POST', body: form })
}

export async function fetchMissionTaskFrames(taskId: string): Promise<StoredMissionFrame[]> {
  const response = await request<{ frames: StoredMissionFrame[] }>(`/api/tasks/${encodeURIComponent(taskId)}/frames`)
  return response.frames
}

export async function deleteMissionTaskFrame(taskId: string, frameId: string): Promise<void> {
  const response = await fetch(`/api/tasks/${encodeURIComponent(taskId)}/frames/${encodeURIComponent(frameId)}`, { method: 'DELETE' })
  if (!response.ok) {
    const payload: unknown = await response.json().catch(() => null)
    const detail = payload && typeof payload === 'object' && 'detail' in payload ? payload.detail : null
    throw new MissionTaskError(typeof detail === 'string' ? detail : '无法移除这个关键帧。', response.status)
  }
}

export async function fetchMissionTaskFrameFile(taskId: string, frameId: string): Promise<Blob> {
  const response = await fetch(`/api/tasks/${encodeURIComponent(taskId)}/frames/${encodeURIComponent(frameId)}`)
  if (!response.ok) throw new MissionTaskError('无法读取当前任务的关键帧文件。', response.status)
  return response.blob()
}

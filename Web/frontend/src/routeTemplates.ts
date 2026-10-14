import type { RoadPreview } from './routeTypes'

export type RouteTemplateAnchor = { id: string; latitude: number; longitude: number }
export type RouteTemplateWaypointSetting = {
  altitude_m: number
  heading_mode: 'followWayline' | 'manually' | 'fixed' | 'smoothTransition' | 'towardPOI'
  heading_angle: number
  gimbal_pitch_angle: number
  export_anchor?: boolean
}

export type RouteTemplatePayload = {
  schema_version: 1
  plan_input: {
    anchors: RouteTemplateAnchor[]
    road_source: 'local' | 'online'
    altitude_m: number
    speed_kmh: number
    clearance_m: number
  }
  route_snapshot: RoadPreview
  waypoint_settings: RouteTemplateWaypointSetting[]
  dji_model: 'fly' | 'm30' | 'm3e'
}

export type RouteTemplateSummary = {
  id: string
  name: string
  description: string
  created_at: string
  updated_at: string
  version: number
  anchor_count?: number
  start?: { latitude: number; longitude: number }
  end?: { latitude: number; longitude: number }
}
export type RouteTemplateRecord = RouteTemplateSummary & { payload: RouteTemplatePayload }
export type CreateRouteTemplateInput = { name: string; description?: string; payload: RouteTemplatePayload }
export type UpdateRouteTemplateInput = CreateRouteTemplateInput & { expected_version: number }

export class RouteTemplateApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message)
    this.name = 'RouteTemplateApiError'
  }
}

const endpoint = '/api/route-templates'

function recordEndpoint(id: string) {
  return `${endpoint}/${encodeURIComponent(id)}`
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    const detail = body && typeof body === 'object' && 'detail' in body ? body.detail : null
    throw new RouteTemplateApiError(
      typeof detail === 'string' && detail.trim() ? detail : '路线模板操作失败，请稍后重试。',
      response.status,
    )
  }
  if (response.status === 204) return undefined as T
  if (!body || typeof body !== 'object') {
    throw new RouteTemplateApiError('路线模板接口返回了无法识别的数据。', response.status)
  }
  return body as T
}

export async function listRouteTemplates(signal?: AbortSignal): Promise<RouteTemplateSummary[]> {
  const result = await request<{ templates: RouteTemplateSummary[] }>(endpoint, { signal })
  if (!Array.isArray(result.templates)) {
    throw new RouteTemplateApiError('路线模板列表数据格式错误。', 200)
  }
  return result.templates
}

export function getRouteTemplate(id: string, signal?: AbortSignal): Promise<RouteTemplateRecord> {
  return request<RouteTemplateRecord>(recordEndpoint(id), { signal })
}

export function createRouteTemplate(input: CreateRouteTemplateInput): Promise<RouteTemplateRecord> {
  return request<RouteTemplateRecord>(endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
}

export function updateRouteTemplate(id: string, input: UpdateRouteTemplateInput): Promise<RouteTemplateRecord> {
  return request<RouteTemplateRecord>(recordEndpoint(id), {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
}

export async function deleteRouteTemplate(id: string): Promise<void> {
  await request<void>(recordEndpoint(id), { method: 'DELETE' })
}

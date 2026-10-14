import { bd09ToWgs84 } from './baiduCoordinates'

export type PlaceResult = {
  name: string
  address: string
  uid: string
  location: { lat: number; lng: number }
  coordinate_system: 'bd09'
  distance_m?: number
  precision?: { precise: number | null; confidence: number | null; level: string | null }
}
function validPlace(item: unknown): item is PlaceResult {
  if (!item || typeof item !== 'object') return false
  const place = item as PlaceResult
  return typeof place.name === 'string' && place.coordinate_system === 'bd09'
    && Number.isFinite(place.location?.lat) && Number.isFinite(place.location?.lng)
    && Math.abs(place.location.lat) <= 90 && Math.abs(place.location.lng) <= 180
}

export function placeToPoint(place: PlaceResult) {
  if (!validPlace(place)) throw new Error('地点坐标无效，请重新搜索。')
  const [longitude, latitude] = bd09ToWgs84([place.location.lng, place.location.lat])
  return { latitude, longitude }
}

export async function fetchPlaces(query: string, region: string, signal: AbortSignal): Promise<PlaceResult[]> {
  const params = new URLSearchParams({ query: query.trim(), region: region.trim() })
  const response = await fetch(`/api/places/search?${params}`, { signal })
  const payload = await response.json().catch(() => null)
  if (!response.ok) throw new Error(typeof payload?.detail === 'string' ? payload.detail : '地点搜索失败，请稍后重试。')
  if (!Array.isArray(payload?.results)) throw new Error('地点搜索返回了无法识别的数据。')
  return payload.results.filter(validPlace)
}

export async function fetchNearbyPlaces(
  query: string,
  center: { latitude: number; longitude: number },
  signal: AbortSignal,
): Promise<PlaceResult[]> {
  const params = new URLSearchParams({
    query: query.trim(),
    latitude: String(center.latitude),
    longitude: String(center.longitude),
  })
  const response = await fetch(`/api/places/around?${params}`, { signal })
  const payload = await response.json().catch(() => null)
  if (!response.ok) throw new Error(typeof payload?.detail === 'string' ? payload.detail : '附近地点搜索失败，请稍后重试。')
  if (!Array.isArray(payload?.results)) throw new Error('附近地点搜索返回了无法识别的数据。')
  return payload.results.filter(validPlace)
}

export async function fetchLocationAddress(
  center: { latitude: number; longitude: number },
  signal: AbortSignal,
): Promise<string> {
  const params = new URLSearchParams({
    latitude: String(center.latitude),
    longitude: String(center.longitude),
  })
  const response = await fetch(`/api/places/reverse?${params}`, { signal })
  const payload = await response.json().catch(() => null)
  if (!response.ok) throw new Error(typeof payload?.detail === 'string' ? payload.detail : '获取当前位置地址失败，请稍后重试。')
  if (typeof payload?.address !== 'string' || !payload.address.trim()) throw new Error('当前位置地址返回了无法识别的数据。')
  return payload.address.trim()
}

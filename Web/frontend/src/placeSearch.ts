import { bd09ToWgs84 } from './baiduCoordinates'

export type PlaceResult = {
  name: string
  address: string
  uid: string
  location: { lat: number; lng: number }
  coordinate_system: 'bd09'
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

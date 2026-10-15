export type LngLat = [number, number]

const A = 6378245
const EE = 0.006693421622965943
const X_PI = Math.PI * 3000 / 180

function inChina([longitude, latitude]: LngLat) {
  return longitude >= 72.004 && longitude <= 137.8347 && latitude >= 0.8293 && latitude <= 55.8271
}

function latitudeOffset(x: number, y: number) {
  let result = -100 + 2 * x + 3 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * Math.sqrt(Math.abs(x))
  result += (20 * Math.sin(6 * x * Math.PI) + 20 * Math.sin(2 * x * Math.PI)) * 2 / 3
  result += (20 * Math.sin(y * Math.PI) + 40 * Math.sin(y / 3 * Math.PI)) * 2 / 3
  return result + (160 * Math.sin(y / 12 * Math.PI) + 320 * Math.sin(y * Math.PI / 30)) * 2 / 3
}

function longitudeOffset(x: number, y: number) {
  let result = 300 + x + 2 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x))
  result += (20 * Math.sin(6 * x * Math.PI) + 20 * Math.sin(2 * x * Math.PI)) * 2 / 3
  result += (20 * Math.sin(x * Math.PI) + 40 * Math.sin(x / 3 * Math.PI)) * 2 / 3
  return result + (150 * Math.sin(x / 12 * Math.PI) + 300 * Math.sin(x / 30 * Math.PI)) * 2 / 3
}

function wgs84ToGcj02(point: LngLat): LngLat {
  if (!inChina(point)) return point
  const [longitude, latitude] = point
  const radianLatitude = latitude / 180 * Math.PI
  const eccentricity = 1 - EE * Math.sin(radianLatitude) ** 2
  const latitudeDelta = latitudeOffset(longitude - 105, latitude - 35) * 180
    / ((A * (1 - EE)) / (eccentricity * Math.sqrt(eccentricity)) * Math.PI)
  const longitudeDelta = longitudeOffset(longitude - 105, latitude - 35) * 180
    / (A / Math.sqrt(eccentricity) * Math.cos(radianLatitude) * Math.PI)
  return [longitude + longitudeDelta, latitude + latitudeDelta]
}

export function wgs84ToBd09(point: LngLat): LngLat {
  const [longitude, latitude] = wgs84ToGcj02(point)
  const radius = Math.hypot(longitude, latitude) + 0.00002 * Math.sin(latitude * X_PI)
  const angle = Math.atan2(latitude, longitude) + 0.000003 * Math.cos(longitude * X_PI)
  return [radius * Math.cos(angle) + 0.0065, radius * Math.sin(angle) + 0.006]
}

export function bd09ToWgs84([longitude, latitude]: LngLat): LngLat {
  const x = longitude - 0.0065
  const y = latitude - 0.006
  const radius = Math.hypot(x, y) - 0.00002 * Math.sin(y * X_PI)
  const angle = Math.atan2(y, x) - 0.000003 * Math.cos(x * X_PI)
  const gcjPoint: LngLat = [radius * Math.cos(angle), radius * Math.sin(angle)]
  if (!inChina(gcjPoint)) return gcjPoint
  const candidate: LngLat = [...gcjPoint]
  for (let iteration = 0; iteration < 4; iteration++) {
    const projected = wgs84ToGcj02(candidate)
    candidate[0] -= projected[0] - gcjPoint[0]
    candidate[1] -= projected[1] - gcjPoint[1]
  }
  return candidate
}

// The app stores WGS84 coordinates. The satellite scene uses BD-09 longitude
// and latitude, including in its ECEF projection. Preserve height/other values.
export function toBaiduSceneCoordinate(coordinate: number[]): number[] {
  return [...wgs84ToBd09([coordinate[0], coordinate[1]]), ...coordinate.slice(2)]
}

export function fromBaiduSceneCoordinate(coordinate: number[]): number[] {
  return [...bd09ToWgs84([coordinate[0], coordinate[1]]), ...coordinate.slice(2)]
}

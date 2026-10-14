/** WGS84 route playback. Progress is a fraction of distance traveled, not vertex count. */

const EARTH_RADIUS_M = 6_371_008.8
const TO_RAD = Math.PI / 180
const TO_DEG = 180 / Math.PI

export type FlightHeadingMode = 'followWayline' | 'manually' | 'fixed' | 'smoothTransition' | 'towardPOI'

export type FlightWaypointSetting = {
  altitude_m?: number | null
  heading_mode?: FlightHeadingMode
  heading_angle?: number | null
  gimbal_pitch_angle?: number | null
}

export type FlightTimeline = {
  readonly coordinates: readonly (readonly [number, number])[]
  readonly cumulativeDistancesM: readonly number[]
  readonly waypointProgress: readonly number[]
  readonly waypointAltitudesM: readonly number[]
  readonly waypointHeadings: readonly number[]
  readonly waypointPitches: readonly number[]
  readonly segmentBearings: readonly number[]
  readonly totalDistanceM: number
  readonly durationSeconds: number
}

export type FlightSample = {
  readonly progress: number
  readonly distanceM: number
  readonly elapsedSeconds: number
  readonly remainingSeconds: number
  readonly longitude: number
  readonly latitude: number
  readonly altitude: number
  readonly heading: number
  readonly pitch: number
  /** The route edge currently being traversed, from this index to index + 1. */
  readonly segmentIndex: number
  readonly segmentFraction: number
  /** Closest route vertex by distance traveled (useful for the node label). */
  readonly waypointIndex: number
}

export type FlightTimelineInput = {
  coordinates: readonly (readonly number[])[]
  waypointSettings: readonly FlightWaypointSetting[]
  speedKmh: number
  defaultAltitudeM: number
}

function normalizeHeading(degrees: number): number {
  return ((degrees % 360) + 360) % 360
}

function lerpAngle(first: number, second: number, fraction: number): number {
  const shortestTurn = ((second - first + 540) % 360) - 180
  return normalizeHeading(first + shortestTurn * fraction)
}

function haversineMeters(first: readonly number[], second: readonly number[]): number {
  const latitude1 = first[1] * TO_RAD
  const latitude2 = second[1] * TO_RAD
  const deltaLatitude = latitude2 - latitude1
  const deltaLongitude = (second[0] - first[0]) * TO_RAD
  const halfChord = Math.sin(deltaLatitude / 2) ** 2
    + Math.cos(latitude1) * Math.cos(latitude2) * Math.sin(deltaLongitude / 2) ** 2
  return 2 * EARTH_RADIUS_M * Math.asin(Math.sqrt(Math.min(1, halfChord)))
}

/** Clockwise degrees from true north, following the WGS84 longitude/latitude order. */
function bearingDegrees(first: readonly number[], second: readonly number[]): number {
  const latitude1 = first[1] * TO_RAD
  const latitude2 = second[1] * TO_RAD
  const deltaLongitude = (second[0] - first[0]) * TO_RAD
  const east = Math.sin(deltaLongitude) * Math.cos(latitude2)
  const north = Math.cos(latitude1) * Math.sin(latitude2)
    - Math.sin(latitude1) * Math.cos(latitude2) * Math.cos(deltaLongitude)
  return normalizeHeading(Math.atan2(east, north) * TO_DEG)
}

function settingNumber(value: number | null | undefined, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

export function createFlightTimeline({
  coordinates: inputCoordinates, waypointSettings, speedKmh, defaultAltitudeM,
}: FlightTimelineInput): FlightTimeline {
  if (inputCoordinates.length < 2) throw new RangeError('A flight route needs at least two points.')
  if (!Number.isFinite(speedKmh) || speedKmh <= 0) throw new RangeError('Flight speed must be positive.')
  if (!Number.isFinite(defaultAltitudeM)) throw new RangeError('Default altitude must be finite.')

  const coordinates = inputCoordinates.map((point): [number, number] => {
    const [longitude, latitude] = point
    if (!Number.isFinite(longitude) || !Number.isFinite(latitude)
      || Math.abs(longitude) > 180 || Math.abs(latitude) > 90) {
      throw new RangeError('Flight route contains an invalid WGS84 coordinate.')
    }
    return [longitude, latitude]
  })
  const cumulativeDistancesM = [0]
  const segmentBearings: number[] = []
  const segmentDistancesM: number[] = []
  for (let index = 0; index < coordinates.length - 1; index++) {
    const first = coordinates[index]
    const second = coordinates[index + 1]
    const distanceM = haversineMeters(first, second)
    segmentDistancesM.push(distanceM)
    cumulativeDistancesM.push(cumulativeDistancesM[index] + distanceM)
    segmentBearings.push(bearingDegrees(first, second))
  }
  const totalDistanceM = cumulativeDistancesM[cumulativeDistancesM.length - 1]
  if (totalDistanceM <= 1e-6) throw new RangeError('Flight route must cover a positive distance.')

  // Resolve each node's forward course in linear time, including coincident
  // route vertices. A terminal node uses the last nonzero inbound bearing.
  const waypointCourses = new Array<number>(coordinates.length)
  let forwardCourse = Number.NaN
  for (let index = coordinates.length - 2; index >= 0; index--) {
    if (segmentDistancesM[index] > 1e-6) forwardCourse = segmentBearings[index]
    waypointCourses[index] = forwardCourse
  }
  let inboundCourse = 0
  for (let index = 1; index < coordinates.length; index++) {
    if (segmentDistancesM[index - 1] > 1e-6) inboundCourse = segmentBearings[index - 1]
    if (!Number.isFinite(waypointCourses[index])) waypointCourses[index] = inboundCourse
  }

  const waypointAltitudesM = coordinates.map((_, index) =>
    settingNumber(waypointSettings[index]?.altitude_m, defaultAltitudeM))
  const waypointPitches = coordinates.map((_, index) =>
    Math.max(-90, Math.min(45, settingNumber(waypointSettings[index]?.gimbal_pitch_angle, 0))))
  const waypointHeadings = coordinates.map((_, index) => {
    const setting = waypointSettings[index]
    const course = waypointCourses[index]
    if (!setting || setting.heading_mode === 'followWayline' || setting.heading_mode === 'manually') return course
    return normalizeHeading(settingNumber(setting.heading_angle, course))
  })

  return {
    coordinates,
    cumulativeDistancesM,
    waypointProgress: cumulativeDistancesM.map(distance => distance / totalDistanceM),
    waypointAltitudesM,
    waypointHeadings,
    waypointPitches,
    segmentBearings,
    totalDistanceM,
    durationSeconds: totalDistanceM / (speedKmh / 3.6),
  }
}

function geodesicPoint(
  first: readonly [number, number], second: readonly [number, number],
  bearing: number, distanceM: number, fraction: number,
): [number, number] {
  if (fraction <= 0) return [first[0], first[1]]
  if (fraction >= 1) return [second[0], second[1]]
  const angularDistance = distanceM * fraction / EARTH_RADIUS_M
  const latitude1 = first[1] * TO_RAD
  const longitude1 = first[0] * TO_RAD
  const course = bearing * TO_RAD
  const latitude = Math.asin(Math.sin(latitude1) * Math.cos(angularDistance)
    + Math.cos(latitude1) * Math.sin(angularDistance) * Math.cos(course))
  const longitude = longitude1 + Math.atan2(
    Math.sin(course) * Math.sin(angularDistance) * Math.cos(latitude1),
    Math.cos(angularDistance) - Math.sin(latitude1) * Math.sin(latitude),
  )
  return [((longitude * TO_DEG + 540) % 360) - 180, latitude * TO_DEG]
}

/** Sample a pose at 0..1; values outside the range are clamped for scrubbing. */
export function sampleFlight(timeline: FlightTimeline, inputProgress: number): FlightSample {
  const progress = Number.isFinite(inputProgress) ? Math.max(0, Math.min(1, inputProgress)) : 0
  const distanceM = timeline.totalDistanceM * progress
  const cumulative = timeline.cumulativeDistancesM

  // Upper bound makes an exact node select its outgoing edge. The terminal
  // node retains its inbound edge so a final sample lands exactly on it.
  let low = 1
  let high = cumulative.length
  while (low < high) {
    const middle = (low + high) >>> 1
    if (cumulative[middle] <= distanceM) low = middle + 1
    else high = middle
  }
  const segmentIndex = Math.min(cumulative.length - 2, low - 1)
  const segmentDistanceM = cumulative[segmentIndex + 1] - cumulative[segmentIndex]
  const segmentFraction = segmentDistanceM > 0
    ? Math.max(0, Math.min(1, (distanceM - cumulative[segmentIndex]) / segmentDistanceM))
    : 1
  const [longitude, latitude] = geodesicPoint(
    timeline.coordinates[segmentIndex], timeline.coordinates[segmentIndex + 1],
    timeline.segmentBearings[segmentIndex], segmentDistanceM, segmentFraction,
  )
  const blend = (values: readonly number[]) =>
    values[segmentIndex] + (values[segmentIndex + 1] - values[segmentIndex]) * segmentFraction
  const elapsedSeconds = timeline.durationSeconds * progress

  return {
    progress,
    distanceM,
    elapsedSeconds,
    remainingSeconds: timeline.durationSeconds - elapsedSeconds,
    longitude,
    latitude,
    altitude: blend(timeline.waypointAltitudesM),
    heading: lerpAngle(timeline.waypointHeadings[segmentIndex], timeline.waypointHeadings[segmentIndex + 1], segmentFraction),
    pitch: blend(timeline.waypointPitches),
    segmentIndex,
    segmentFraction,
    waypointIndex: segmentFraction < 0.5 ? segmentIndex : segmentIndex + 1,
  }
}

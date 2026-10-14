import assert from 'node:assert/strict'
import { createFlightTimeline, sampleFlight } from '../src/flightPlayback.ts'

function near(actual, expected, tolerance = 1e-8) {
  assert.ok(Math.abs(actual - expected) <= tolerance, `${actual} should be within ${tolerance} of ${expected}`)
}

const unequalLegs = createFlightTimeline({
  coordinates: [[0, 0], [0.001, 0], [0.004, 0]],
  waypointSettings: [
    { altitude_m: 100, heading_mode: 'fixed', heading_angle: 45, gimbal_pitch_angle: 0 },
    { altitude_m: 180, heading_mode: 'fixed', heading_angle: 120, gimbal_pitch_angle: -30 },
    { altitude_m: 240, heading_mode: 'fixed', heading_angle: 160, gimbal_pitch_angle: -45 },
  ],
  speedKmh: 36,
  defaultAltitudeM: 100,
})
near(unequalLegs.waypointProgress[1], 0.25)
near(unequalLegs.durationSeconds, unequalLegs.totalDistanceM / 10)

const middleOfTrip = sampleFlight(unequalLegs, 0.5)
assert.equal(middleOfTrip.segmentIndex, 1, 'Half the distance lies on the longer second leg')
near(middleOfTrip.longitude, 0.002)
near(middleOfTrip.latitude, 0)
near(middleOfTrip.elapsedSeconds, unequalLegs.durationSeconds / 2)

const middleNode = sampleFlight(unequalLegs, unequalLegs.waypointProgress[1])
assert.equal(middleNode.waypointIndex, 1)
near(middleNode.longitude, 0.001)
assert.equal(middleNode.altitude, 180)
assert.equal(middleNode.heading, 120)
assert.equal(middleNode.pitch, -30)

const wrap = createFlightTimeline({
  coordinates: [[0, 0], [0.001, 0]],
  waypointSettings: [
    { altitude_m: 100, heading_mode: 'fixed', heading_angle: 359, gimbal_pitch_angle: -5 },
    { altitude_m: 200, heading_mode: 'smoothTransition', heading_angle: 1, gimbal_pitch_angle: -45 },
  ],
  speedKmh: 36,
  defaultAltitudeM: 100,
})
const turning = sampleFlight(wrap, 0.5)
near(turning.heading, 0)
assert.equal(turning.altitude, 150)
assert.equal(turning.pitch, -25)

const west = createFlightTimeline({
  coordinates: [[0, 0], [-0.001, 0]],
  waypointSettings: [{ heading_mode: 'followWayline' }, { heading_mode: 'followWayline' }],
  speedKmh: 36,
  defaultAltitudeM: 100,
})
const destination = sampleFlight(west, 1)
near(destination.longitude, -0.001)
near(destination.heading, 270)
assert.equal(destination.waypointIndex, 1)

const input = { waypointSettings: [], speedKmh: 36, defaultAltitudeM: 100 }
assert.throws(() => createFlightTimeline({ ...input, coordinates: [[0, 0]] }), RangeError)
assert.throws(() => createFlightTimeline({ ...input, coordinates: [[0, 0], [0, 0]] }), RangeError)
assert.throws(() => createFlightTimeline({ ...input, speedKmh: 0, coordinates: [[0, 0], [1, 0]] }), RangeError)
assert.throws(() => createFlightTimeline({ ...input, coordinates: [[0, 0], [1, 91]] }), RangeError)

console.log('Flight playback timeline checks passed')

import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import ts from 'typescript'

const source = await readFile(new URL('../src/baiduCoordinates.ts', import.meta.url), 'utf8')
const context = vm.createContext({ exports: {}, Math })
new vm.Script(ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText).runInContext(context)
const { wgs84ToBd09, bd09ToWgs84, toBaiduSceneCoordinate, fromBaiduSceneCoordinate } = context.exports
const points = [[119.9515, 31.6792], [116.404, 39.915], [114.0579, 22.5431]]
for (const point of points) {
  const converted = wgs84ToBd09(point)
  const restored = bd09ToWgs84(converted)
  assert.ok(Math.abs(restored[0] - point[0]) < 0.00001)
  assert.ok(Math.abs(restored[1] - point[1]) < 0.00001)
  assert.ok(Math.abs(converted[0] - point[0]) > 0.005)
}
const outsideChina = [10, 10]
const outsideRestored = bd09ToWgs84(wgs84ToBd09(outsideChina))
assert.ok(Math.abs(outsideRestored[0] - outsideChina[0]) < 0.000001)
assert.ok(Math.abs(outsideRestored[1] - outsideChina[1]) < 0.000001)
const campus = [119.9515, 31.6792, 100]
const sceneCampus = toBaiduSceneCoordinate(campus)
assert.equal(sceneCampus[2], 100)
const restoredCampus = fromBaiduSceneCoordinate(sceneCampus)
assert.ok(Math.abs(restoredCampus[0] - campus[0]) < 0.000001)
assert.ok(Math.abs(restoredCampus[1] - campus[1]) < 0.000001)
assert.equal(restoredCampus[2], campus[2])
assert.deepEqual(campus, [119.9515, 31.6792, 100], 'Rendering must not alter stored mission coordinates')
console.log('Passed: WGS84/BD-09 conversions in and outside China.')

import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import ts from 'typescript'

const source = await readFile(new URL('../src/photoAnalysis.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText

const calls = []
const responses = []
const reply = (status, body) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
})
class StubFormData {
  constructor() { this.entries = [] }
  append(key, value, name) { this.entries.push({ key, value, name }) }
}
const context = vm.createContext({
  exports: {},
  fetch: async (url, init) => {
    calls.push({ url, init })
    assert.ok(responses.length, 'Each request needs a queued response')
    return responses.shift()
  },
  encodeURIComponent,
  JSON,
  FormData: StubFormData,
})
new vm.Script(compiled, { filename: 'photoAnalysis.ts' }).runInContext(context)
const api = context.exports

const health = {
  available: true, busy: false, python: 'D:/anaconda/envs/waterseg/python.exe',
  model_dir: 'D:/低空经济/model/artifacts/onnx', max_photos_per_job: 200, message: '照片分析组件已就绪。',
}
responses.push(reply(200, health))
const fetchedHealth = await api.fetchPhotoAnalysisHealth()
assert.equal(fetchedHealth.available, true)
assert.equal(fetchedHealth.max_photos_per_job, 200)
assert.equal(calls.at(-1).url, '/api/photo-analysis/health')

responses.push(reply(202, { job_id: '20261015-photo', job_url: '/api/photo-analysis/jobs/20261015-photo' }))
const submitted = await api.submitPhotoAnalysis(
  [{ name: 'pond.jpg' }, { name: 'road.jpg' }],
  { site_name: 's1', note: '雨后' },
)
assert.equal(submitted.job_id, '20261015-photo')
assert.equal(calls.at(-1).url, '/api/photo-analysis/jobs')
assert.equal(calls.at(-1).init.method, 'POST')
const form = calls.at(-1).init.body
assert.equal(form.entries.filter((entry) => entry.key === 'photos').length, 2)
assert.equal(form.entries.find((entry) => entry.key === 'site_name').value, 's1')
assert.equal(form.entries.find((entry) => entry.key === 'note').value, '雨后')
assert.equal(form.entries.filter((entry) => entry.key === 'photos')[0].name, 'pond.jpg')

const image = {
  name: '000-pond.jpg', sha256: 'a'.repeat(64), width: 4000, height: 3000, model_input_size: 256,
  suspected_pixels: 120000, suspected_fraction: 0.01, suspected_patch_count: 3,
  mean_confidence_on_suspected: 0.87, overlay: '000-pond__overlay.png',
  suspected_mask: '000-pond__suspected.png', confidence_map: '000-pond__confidence.png',
}
const jobDone = {
  job_id: '20261015-photo', state: 'done', created_at: '2026-10-15T02:00:00Z', finished_at: '2026-10-15T02:00:05Z',
  context: { site_name: 's1' },
  inputs: [{ name: '000-pond.jpg', original_name: 'pond.jpg', bytes: 1024 }],
  results: {
    model: { default_model: 'abcdwavenet_ponding.onnx', task: 'ponding', water_class: 1, input_size: 256, normalize: 'div255', model_sha256: 'b'.repeat(64) },
    images: [image], elapsed_seconds: 4.2, interpretation: '疑似积水像素是待人工核查的线索。',
  },
  error: null,
}
responses.push(reply(200, jobDone))
const job = await api.fetchPhotoAnalysisJob('20261015-photo')
assert.equal(job.state, 'done')
assert.equal(job.results.images[0].suspected_patch_count, 3)
assert.match(job.results.interpretation, /人工核查/)
assert.equal(calls.at(-1).url, '/api/photo-analysis/jobs/20261015-photo')

const fileUrl = api.photoAnalysisFileUrl('20261015-photo', '000-pond__overlay.png')
assert.equal(fileUrl, '/api/photo-analysis/jobs/20261015-photo/files/000-pond__overlay.png')
const slashUrl = api.photoAnalysisFileUrl('job/1', 'inputs/000-pond.jpg')
assert.equal(slashUrl, '/api/photo-analysis/jobs/job%2F1/files/inputs%2F000-pond.jpg')

responses.push(reply(404, { detail: '找不到该分析任务。' }))
await assert.rejects(api.fetchPhotoAnalysisJob('missing'), error =>
  error.name === 'PhotoAnalysisApiError' && error.status === 404 && /找不到/.test(error.message))

responses.push(reply(500, null))
await assert.rejects(api.fetchPhotoAnalysisHealth(), error => error.status === 500 && error.message.length > 0)

responses.push(reply(200, { unexpected: true }))
await assert.rejects(api.fetchPhotoAnalysisHealth(), error => /无法识别/.test(error.message))

console.log('Passed: photo analysis health, multipart submit, job polling, file URLs, and error mapping.')

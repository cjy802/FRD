/**
 * 识别流水线全局编排（模块级单例，页面级生命周期）。
 *
 * 后端 job 与前端连接无关，缺的只是"页面内切任务/切模块"时的推进与恢复：
 * 推进下一级（YOLO→GLM）一律使用启动链时快照的 taskId，绝不读当前面板
 * props——根治切任务瞬间 GLM 复检被丢弃或挂错任务的竞态。页面关闭则停止
 * 推进（后端把当前 job 跑完）；重开页面后由 resumeTaskPipeline 检测断链
 * 并补交（startChain 轮询到 done 会立即推进缺失的 GLM 终审）。
 */
import { reactive } from 'vue'
import { fetchPhotoAnalysisJob, submitPhotoAnalysisJob, type PhotoAnalysisJob } from './photoAnalysis'

export const PIPELINE_STAGES: Array<{ id: string; short: string; noun: string }> = [
  { id: 'yolo-champion', short: 'YOLO 初筛', noun: 'YOLO' },
  { id: 'remote-glm-puddle', short: 'GLM 终审', noun: 'GLM' },
]

export const STAGE_LABELS: Record<string, string> = Object.fromEntries(
  PIPELINE_STAGES.map(stage => [stage.id, stage.short]),
)

export function stageIndexOfModel(modelId?: string | null): number {
  const index = PIPELINE_STAGES.findIndex(stage => stage.id === modelId)
  return index >= 0 ? index : 0
}

export type PipelineEvent =
  | { type: 'job-submitted'; taskId: string; jobId: string; stageIndex: number }
  | { type: 'job-update'; taskId: string; job: PhotoAnalysisJob }
  | { type: 'chain-finished'; taskId: string; job: PhotoAnalysisJob; early: boolean }
  | { type: 'chain-stopped'; taskId: string; reason: string }

type Listener = (event: PipelineEvent) => void
const listeners = new Set<Listener>()

export function onPipelineEvent(listener: Listener): () => void {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

function emit(event: PipelineEvent): void {
  for (const listener of [...listeners]) {
    try {
      listener(event)
    } catch {
      // 订阅方异常不拖累编排循环。
    }
  }
}

export const pipelineState = reactive({
  taskId: '',
  jobId: '',
  stageIndex: 0,
  running: false,
  done: 0,
  failed: 0,
  total: 0,
})

/** 单槽被其他任务占用时的续跑排队（chain 终态后自动 drain）。 */
export const pipelineQueuedTaskIds = reactive<string[]>([])

const POLL_MS = 2500
const FAIL_LIMIT = 5
const SUBMIT_RETRY_MS = 5000
const SUBMIT_RETRY_MAX = 12

let timer: number | null = null
let chainEpoch = 0
let chainStagesCount = PIPELINE_STAGES.length
let consecutiveFailures = 0

function stopTimer(): void {
  if (timer !== null) {
    window.clearTimeout(timer)
    timer = null
  }
}

function schedule(epoch: number, delay = POLL_MS): void {
  if (epoch !== chainEpoch) return
  stopTimer()
  timer = window.setTimeout(() => { void pollOnce(epoch) }, delay)
}

function endChain(): void {
  pipelineState.running = false
  stopTimer()
  drainPendingResume()
}

type ResumeRequest = { taskId: string; jobId: string; stageIndex: number }
const pendingResume: ResumeRequest[] = []

function drainPendingResume(): void {
  const next = pendingResume.shift()
  if (next) startChain(next.taskId, next.jobId, next.stageIndex)
}

function requestResume(request: ResumeRequest): 'started' | 'ignored' | 'busy' {
  if (pipelineState.running) {
    if (pipelineState.taskId === request.taskId) return 'ignored'
    if (!pendingResume.some(entry => entry.taskId === request.taskId)) {
      pendingResume.push(request)
      pipelineQueuedTaskIds.splice(0, pipelineQueuedTaskIds.length, ...pendingResume.map(entry => entry.taskId))
    }
    return 'busy'
  }
  startChain(request.taskId, request.jobId, request.stageIndex)
  return 'started'
}

export function startChain(taskId: string, jobId: string, stageIndex = 0, stagesCount = PIPELINE_STAGES.length): void {
  chainEpoch += 1
  stopTimer()
  consecutiveFailures = 0
  // GLM 等末级未配置时按可用级数收口，避免对着不可用模型反复提交。
  chainStagesCount = Math.min(Math.max(stagesCount, 1), PIPELINE_STAGES.length)
  pipelineQueuedTaskIds.splice(0, pipelineQueuedTaskIds.length, ...pendingResume.map(entry => entry.taskId))
  Object.assign(pipelineState, { taskId, jobId, stageIndex, running: true, done: 0, failed: 0, total: 0 })
  schedule(chainEpoch)
}

async function submitNextStage(epoch: number, taskId: string, prevJobId: string, job: PhotoAnalysisJob, nextIndex: number): Promise<void> {
  const stage = PIPELINE_STAGES[nextIndex]
  if (!stage) return
  const suspectedNames = new Set((job.results?.images ?? [])
    .filter(image => (image.suspected_pixels ?? 0) > 0).map(image => image.name))
  const frameIds = (job.items ?? []).filter(item => suspectedNames.has(item.name)).map(item => item.frame_id)
  if (!frameIds.length) {
    emit({ type: 'chain-finished', taskId, job, early: true })
    endChain()
    return
  }
  for (let attempt = 0; attempt <= SUBMIT_RETRY_MAX; attempt += 1) {
    if (epoch !== chainEpoch) return
    try {
      const submitted = await submitPhotoAnalysisJob({ taskId, frameIds, modelId: stage.id, recheckOfJobId: prevJobId })
      if (epoch !== chainEpoch) return
      pipelineState.stageIndex = nextIndex
      pipelineState.jobId = submitted.job_id
      pipelineState.done = 0
      pipelineState.failed = 0
      pipelineState.total = frameIds.length
      emit({ type: 'job-submitted', taskId, jobId: submitted.job_id, stageIndex: nextIndex })
      schedule(epoch)
      return
    } catch (cause) {
      if (epoch !== chainEpoch) return
      const message = cause instanceof Error ? cause.message : String(cause)
      // 单槽被占（别的任务在识别）→ 等待重试；其他错误不硬撑。
      const retryable = /409|已有分析任务/.test(message)
      if (!retryable || attempt === SUBMIT_RETRY_MAX) {
        emit({ type: 'chain-stopped', taskId, reason: `${stage.noun}提交失败：${message}` })
        endChain()
        return
      }
      await new Promise(resolve => window.setTimeout(resolve, SUBMIT_RETRY_MS))
    }
  }
}

async function pollOnce(epoch: number): Promise<void> {
  if (epoch !== chainEpoch || !pipelineState.running) return
  const { taskId, jobId, stageIndex } = pipelineState
  let job: PhotoAnalysisJob
  try {
    job = await fetchPhotoAnalysisJob(jobId)
    consecutiveFailures = 0
  } catch {
    if (epoch !== chainEpoch) return
    consecutiveFailures += 1
    if (consecutiveFailures >= FAIL_LIMIT) {
      emit({ type: 'chain-stopped', taskId, reason: '连续多次无法读取识别进度' })
      endChain()
      return
    }
    schedule(epoch)
    return
  }
  if (epoch !== chainEpoch) return
  pipelineState.done = job.counts?.done ?? pipelineState.done
  pipelineState.failed = job.counts?.failed ?? pipelineState.failed
  pipelineState.total = job.counts?.total ?? pipelineState.total
  emit({ type: 'job-update', taskId, job })
  if (job.state === 'pending' || job.state === 'running') {
    schedule(epoch)
    return
  }
  if (job.state !== 'done') {
    emit({ type: 'chain-stopped', taskId, reason: job.state === 'error' ? `识别任务出错：${job.error ?? '未知错误'}` : '识别任务已取消' })
    endChain()
    return
  }
  const nextIndex = stageIndex + 1
  if (nextIndex < chainStagesCount) {
    await submitNextStage(epoch, taskId, jobId, job, nextIndex)
    return
  }
  emit({ type: 'chain-finished', taskId, job, early: false })
  endChain()
}

export type ResumeOutcome = 'none' | 'running' | 'queued' | 'resumed' | 'finished' | 'error'

/** 切回任务时检测识别链状态：断链（初筛 done 但没提交过复检）则补交，未完则接管轮询。 */
export async function resumeTaskPipeline(taskId: string, jobIds: string[]): Promise<ResumeOutcome> {
  if (!taskId || !jobIds.length) return 'none'
  if (pipelineState.running && pipelineState.taskId === taskId) return 'running'
  const lastId = jobIds[jobIds.length - 1]
  try {
    const last = await fetchPhotoAnalysisJob(lastId)
    if (last.state === 'pending' || last.state === 'running') {
      const stage = last.context?.recheck_of_job_id ? 1 : stageIndexOfModel(last.model_id)
      const how = requestResume({ taskId, jobId: lastId, stageIndex: stage })
      return how === 'busy' ? 'queued' : 'running'
    }
    if (last.state !== 'done') return 'finished'
    if (last.context?.recheck_of_job_id || last.model_id !== PIPELINE_STAGES[0].id) return 'finished'
    // 末位 job 是初筛且已完成：查历史 job 里有没有指向它的复检（job 总数很少，逐个查）。
    let hasRecheck = false
    for (const id of jobIds.slice(0, -1)) {
      if (id === lastId) continue
      try {
        const job = await fetchPhotoAnalysisJob(id)
        if (job.context?.recheck_of_job_id === lastId) {
          hasRecheck = true
          break
        }
      } catch {
        // 单个历史 job 查不到按"无复检"继续，最坏情况是被后端去重或重复提交一次复检。
      }
    }
    if (hasRecheck) return 'finished'
    // startChain 第一次轮询就会发现 done 并自动补交 GLM（无疑似帧则自然提前结束）。
    const how = requestResume({ taskId, jobId: lastId, stageIndex: 0 })
    return how === 'busy' ? 'queued' : 'resumed'
  } catch {
    return 'error'
  }
}

if (typeof document !== 'undefined') {
  document.addEventListener('visibilitychange', () => {
    // 后台标签页 setTimeout 会被浏览器节流；回到前台立即拉一次，避免推进长时间停滞。
    if (document.visibilityState === 'visible' && pipelineState.running) schedule(chainEpoch, 0)
  })
}

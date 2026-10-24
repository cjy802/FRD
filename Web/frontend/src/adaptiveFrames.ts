// Client-side adaptive frame extraction from the LOCAL video file.
// Mirrors backend/scripts/run_adaptive_frames.py (SRT course change + visual
// overlap + blur filter), except the overlap metric is block-matching
// translation coverage instead of ORB homography - good enough for nadir
// drone footage, and nothing ever leaves the machine.
//
// Two passes keep the main thread responsive: pass 1 scans small canvases
// only (no 4K->1920 downscale per candidate) and picks timestamps; pass 2
// re-seeks just the selected timestamps to export full-size JPEGs.

import { interpolateVideoPosition, type VideoTelemetryPoint } from './videoSurvey'

export type ClientAdaptiveOptions = {
  maxFrames: number
  scanFps: number
  straightOverlapPercent: number
  turnOverlapPercent: number
  turnAngleDegrees: number
  cameraChangeDegrees: number
  blurFilterEnabled: boolean
  minimumSharpnessPercent: number
}

export type ClientSelectedFrame = {
  file: File
  time_seconds: number
  latitude: number
  longitude: number
  altitude_m: number | null
  gimbal_pitch_deg: number | null
  selection_reason: string
  estimated_overlap_percent: number | null
  turn_angle_deg: number | null
  sharpness_ratio_percent: number
}

export type ClientAdaptiveProgress = {
  scanned: number
  total: number
  selected: number
  blurred: number
  blank: number
  exported: number
}

export class ExtractionCancelled extends Error {
  constructor() {
    super('cancelled')
  }
}

function makeCanvas(width: number, height: number) {
  const canvas = document.createElement('canvas')
  canvas.width = width
  canvas.height = height
  return canvas
}

function grayData(canvas: HTMLCanvasElement): Uint8ClampedArray {
  const context = canvas.getContext('2d', { willReadFrequently: true })!
  const image = context.getImageData(0, 0, canvas.width, canvas.height).data
  const gray = new Uint8ClampedArray(canvas.width * canvas.height)
  for (let index = 0, pixel = 0; pixel < gray.length; index += 4, pixel += 1) {
    gray[pixel] = (image[index] * 299 + image[index + 1] * 587 + image[index + 2] * 114) / 1000
  }
  return gray
}

// Laplacian variance - the same sharpness metric the backend uses.
function laplacianVariance(gray: Uint8ClampedArray, width: number, height: number): number {
  let sum = 0
  let sumSquares = 0
  let count = 0
  for (let y = 1; y < height - 1; y += 1) {
    const row = y * width
    for (let x = 1; x < width - 1; x += 1) {
      const center = gray[row + x]
      const response = gray[row - width + x] + gray[row + width + x] + gray[row + x - 1] + gray[row + x + 1] - 4 * center
      sum += response
      sumSquares += response * response
      count += 1
    }
  }
  const mean = sum / count
  return sumSquares / count - mean * mean
}

// Coverage of the anchor frame still visible in the current frame, estimated
// by scanning integer translations for the best block match. Mean brightness
// is removed per frame so exposure drift (snow, auto-exposure) cannot fake
// or break matches.
function translationCoverage(anchor: Uint8ClampedArray, current: Uint8ClampedArray,
  width: number, height: number, radiusX: number, radiusY: number, sadThreshold: number): number {
  let bestCoverage = 0
  let bestSad = Number.POSITIVE_INFINITY
  const total = width * height
  let anchorMean = 0
  for (let index = 0; index < total; index += 1) anchorMean += anchor[index]
  anchorMean /= total
  let currentMean = 0
  for (let index = 0; index < total; index += 1) currentMean += current[index]
  currentMean /= total
  for (let dy = -radiusY; dy <= radiusY; dy += 1) {
    for (let dx = -radiusX; dx <= radiusX; dx += 1) {
      let sad = 0
      let count = 0
      for (let y = Math.max(0, -dy); y < Math.min(height, height - dy); y += 1) {
        const anchorRow = y * width
        const currentRow = (y + dy) * width
        for (let x = Math.max(0, -dx); x < Math.min(width, width - dx); x += 1) {
          sad += Math.abs((anchor[anchorRow + x] - anchorMean) - (current[currentRow + x + dx] - currentMean))
          count += 1
        }
      }
      if (!count) continue
      const meanSad = sad / count
      const coverage = count / total * 100
      if (meanSad < bestSad) {
        bestSad = meanSad
        bestCoverage = meanSad <= sadThreshold ? coverage : 0
      } else if (meanSad <= sadThreshold && coverage > bestCoverage) {
        bestCoverage = coverage
      }
    }
  }
  return bestCoverage
}

function bearingDeg(first: VideoTelemetryPoint, second: VideoTelemetryPoint): number {
  const latitude1 = first.latitude * Math.PI / 180
  const latitude2 = second.latitude * Math.PI / 180
  const deltaLongitude = (second.longitude - first.longitude) * Math.PI / 180
  const y = Math.sin(deltaLongitude) * Math.cos(latitude2)
  const x = Math.cos(latitude1) * Math.sin(latitude2) - Math.sin(latitude1) * Math.cos(latitude2) * Math.cos(deltaLongitude)
  return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360
}

function distanceMetres(first: VideoTelemetryPoint, second: VideoTelemetryPoint): number {
  const meanLatitude = ((first.latitude + second.latitude) / 2) * Math.PI / 180
  const dx = (second.longitude - first.longitude) * Math.cos(meanLatitude) * 111320
  const dy = (second.latitude - first.latitude) * 111320
  return Math.hypot(dx, dy)
}

function bearingChange(points: VideoTelemetryPoint[], seconds: number, window = 1.5): number | null {
  // 转弯 = 前后两段航向的夹角；低速/悬停（一段不足 1 米）时方位角全是噪声，判为直线。
  const before = interpolateVideoPosition(points, seconds - window)
  const middle = interpolateVideoPosition(points, seconds)
  const after = interpolateVideoPosition(points, seconds + window)
  if (!before || !middle || !after) return null
  if (distanceMetres(before, middle) < 1 || distanceMetres(middle, after) < 1) return null
  const firstLeg = bearingDeg(before, middle)
  const secondLeg = bearingDeg(middle, after)
  return Math.abs((secondLeg - firstLeg + 180) % 360 - 180)
}

function seekOnce(video: HTMLVideoElement, seconds: number, timeoutMs: number): Promise<void> {
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      video.removeEventListener('seeked', onSeeked)
      video.removeEventListener('error', onError)
      window.clearTimeout(timer)
    }
    const onSeeked = () => { cleanup(); resolve() }
    const onError = () => { cleanup(); reject(new Error('seek error')) }
    const timer = window.setTimeout(() => { cleanup(); reject(new Error('seek timeout')) }, timeoutMs)
    video.addEventListener('seeked', onSeeked)
    video.addEventListener('error', onError)
    video.currentTime = seconds
  })
}

// Seek with one retry; a failed seek skips the candidate instead of aborting
// the whole extraction (4K clips under load can take seconds per seek).
async function seekTo(video: HTMLVideoElement, seconds: number): Promise<boolean> {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      await seekOnce(video, seconds, 10000)
      return true
    } catch (error) {
      if (attempt === 1) return false
    }
  }
  return false
}

function yieldToMainThread(): Promise<void> {
  return new Promise(resolve => requestAnimationFrame(() => resolve()))
}

export async function extractAdaptiveFramesClient(
  video: File,
  telemetry: VideoTelemetryPoint[],
  options: ClientAdaptiveOptions,
  onProgress: (progress: ClientAdaptiveProgress) => void,
  isCancelled: () => boolean,
  onFrameSelected?: (frame: ClientSelectedFrame) => void,
): Promise<ClientSelectedFrame[]> {
  if (telemetry.length < 2) throw new Error('SRT 中可用的 GPS 记录不足，无法判断飞行方向和转弯。')
  const url = URL.createObjectURL(video)
  const element = document.createElement('video')
  element.muted = true
  element.preload = 'auto'
  element.src = url
  // 导出专用第二个解码器：扫描在 element 上继续，选中帧立刻由 exporter
  // 导出上传，识别与抽帧保持流水线并行（侧栏播放器此时已暂停）。
  const exporter = document.createElement('video')
  exporter.muted = true
  exporter.preload = 'auto'
  exporter.src = url
  try {
    await new Promise<void>((resolve, reject) => {
      const timer = window.setTimeout(() => reject(new Error('无法读取本地视频。')), 12000)
      element.addEventListener('loadedmetadata', () => { window.clearTimeout(timer); resolve() }, { once: true })
      element.addEventListener('error', () => { window.clearTimeout(timer); reject(new Error('无法读取本地视频。')) }, { once: true })
    })
    await new Promise<void>(resolve => {
      const timer = window.setTimeout(resolve, 12000)
      exporter.addEventListener('loadedmetadata', () => { window.clearTimeout(timer); resolve() }, { once: true })
      exporter.addEventListener('error', () => { window.clearTimeout(timer); resolve() }, { once: true })
    })
    const duration = Number.isFinite(element.duration) ? element.duration : 0
    if (duration <= 0) throw new Error('无法读取本地视频时长。')
    const videoWidth = element.videoWidth || 1920
    const videoHeight = element.videoHeight || 1080

    // 扫描链路 video→960→480：一步 8 倍缩放会丢细节、清晰度骤降，
    // 导致大量帧被模糊过滤误杀；保留两级缩小。
    const midCanvas = makeCanvas(960, Math.max(2, Math.round(960 * videoHeight / videoWidth)))
    const midContext = midCanvas.getContext('2d', { willReadFrequently: true })!
    const workCanvas = makeCanvas(480, Math.max(2, Math.round(480 * videoHeight / videoWidth)))
    const smallCanvas = makeCanvas(96, Math.max(2, Math.round(96 * videoHeight / videoWidth)))
    const workContext = workCanvas.getContext('2d', { willReadFrequently: true })!
    const smallContext = smallCanvas.getContext('2d', { willReadFrequently: true })!

    // 等待真实画面呈现：快速 seek 后立即 draw 常拿到黑帧/上一帧。
    async function settleAfterSeek(target: HTMLVideoElement): Promise<void> {
      const anyVideo = target as HTMLVideoElement & {
        requestVideoFrameCallback?: (callback: () => void) => number
      }
      if (typeof anyVideo.requestVideoFrameCallback === 'function') {
        await new Promise<void>(resolve => {
          const timer = window.setTimeout(resolve, 350)
          anyVideo.requestVideoFrameCallback(() => { window.clearTimeout(timer); resolve() })
        })
      }
      await new Promise(resolve => window.setTimeout(resolve, 50))
    }

    type Candidate = {
      seconds: number
      position: VideoTelemetryPoint
      reason: string
      overlap: number | null
      turnChange: number | null
      sharpnessRatio: number
    }
    const candidates: Candidate[] = []
    const pendingExports: Candidate[] = []
    const selected: ClientSelectedFrame[] = []
    let scanDone = false
    let step = 1 / options.scanFps
    // ceil 而不是 floor：末尾不足一个步长的片段也要有候选，否则视频最后
    // 一段永远扫不到（900 上限分支会把步长拉大，漏的可达数秒）。
    let total = Math.ceil(duration / step)
    if (total > 900) { total = 900; step = duration / total }
    // 候选秒统一 clamp 到末帧前一帧间隔：seek 到 duration 本身部分浏览器会挂起。
    const maxCandidateSeconds = Math.max(0, duration - 1 / 30)
    const lastTelemetry = telemetry.length ? telemetry[telemetry.length - 1] : null
    let blurred = 0
    let blank = 0
    let scanned = 0

    const saveScale = Math.min(1, 1920 / Math.max(videoWidth, videoHeight))
    const saveCanvas = makeCanvas(Math.max(2, Math.round(videoWidth * saveScale)), Math.max(2, Math.round(videoHeight * saveScale)))
    const saveContext = saveCanvas.getContext('2d', { willReadFrequently: true })
    if (!saveContext) return selected

    // 导出 worker：与扫描并行，选中一帧导出一帧、立即回调上传识别。
    const exportWorker = (async () => {
      while (!isCancelled()) {
        const candidate = pendingExports.shift()
        if (!candidate) {
          if (scanDone) return
          await new Promise(resolve => window.setTimeout(resolve, 30))
          continue
        }
        if (!(await seekTo(exporter, candidate.seconds))) continue
        await settleAfterSeek(exporter)
        saveContext.drawImage(exporter, 0, 0, saveCanvas.width, saveCanvas.height)
        try {
          const blob = await new Promise<Blob>((resolve, reject) => saveCanvas.toBlob(
            value => value ? resolve(value) : reject(new Error('截取视频帧失败。')), 'image/jpeg', 0.92))
          const frame: ClientSelectedFrame = {
            file: new File([blob], `frame-${String(Math.floor(candidate.seconds / 60)).padStart(2, '0')}m${String(Math.round(candidate.seconds % 60)).padStart(2, '0')}s.jpg`, { type: 'image/jpeg' }),
            time_seconds: Math.round(candidate.seconds * 1000) / 1000,
            latitude: candidate.position.latitude,
            longitude: candidate.position.longitude,
            altitude_m: candidate.position.altitude_m,
            gimbal_pitch_deg: candidate.position.gimbal_pitch_deg,
            selection_reason: candidate.reason,
            estimated_overlap_percent: candidate.overlap === null ? null : Math.round(candidate.overlap * 10) / 10,
            turn_angle_deg: candidate.turnChange === null ? null : Math.round(candidate.turnChange * 10) / 10,
            sharpness_ratio_percent: Math.round(candidate.sharpnessRatio * 10) / 10,
          }
          selected.push(frame)
          onFrameSelected?.(frame)
          onProgress({ scanned, total, selected: candidates.length, blurred, blank, exported: selected.length })
        } catch { /* 单帧导出失败跳过 */ }
        await yieldToMainThread()
      }
    })()
    let anchorGray: Uint8ClampedArray | null = null
    let anchorPitch: number | null = null
    const sharpnessHistory: number[] = []
    let lastSelectedSeconds: number | null = null

    // —— 第一遍：小图扫描，只决定哪些时刻成为关键帧 ——
    let previousPlannedSeconds = -1
    for (let index = 0; index < total && candidates.length < options.maxFrames; index += 1) {
      if (isCancelled()) throw new ExtractionCancelled()
      const seconds = Math.min(index * step, maxCandidateSeconds)
      if (seconds <= previousPlannedSeconds) continue // clamp 可能让最后两个候选重合
      previousPlannedSeconds = seconds
      let position = interpolateVideoPosition(telemetry, seconds)
      if (!position && lastTelemetry && seconds > lastTelemetry.time_seconds) {
        // SRT 常比视频早结束：尾部沿用最后一个遥测点的位置，不能静默丢帧。
        position = { ...lastTelemetry, time_seconds: seconds }
      }
      if (!position) continue
      if (!(await seekTo(element, seconds))) continue
      await settleAfterSeek(element)
      midContext.drawImage(element, 0, 0, midCanvas.width, midCanvas.height)
      workContext.drawImage(midCanvas, 0, 0, workCanvas.width, workCanvas.height)
      smallContext.drawImage(workCanvas, 0, 0, smallCanvas.width, smallCanvas.height)
      const workGray = grayData(workCanvas)
      const smallGray = grayData(smallCanvas)
      scanned += 1

      // 未解码/黑帧：亮度过低的画面既不能当关键帧也不能参与匹配。
      let brightness = 0
      for (let index2 = 0; index2 < workGray.length; index2 += 1) brightness += workGray[index2]
      brightness /= workGray.length
      if (brightness < 14) {
        blank += 1
        onProgress({ scanned, total, selected: candidates.length, blurred, blank, exported: selected.length })
        await yieldToMainThread()
        continue
      }

      const sharpness = laplacianVariance(workGray, workCanvas.width, workCanvas.height)
      const baseline = sharpnessHistory.length
        ? [...sharpnessHistory].sort((a, b) => a - b)[Math.floor(sharpnessHistory.length / 2)]
        : sharpness
      const sharpnessRatio = sharpness / Math.max(baseline, 1e-6) * 100
      const isBlurry = options.blurFilterEnabled && sharpnessHistory.length > 0
        && sharpnessRatio < options.minimumSharpnessPercent
      sharpnessHistory.push(sharpness)
      if (isBlurry) {
        blurred += 1
        onProgress({ scanned, total, selected: candidates.length, blurred, blank, exported: selected.length })
        await yieldToMainThread()
        continue
      }

      let reason = ''
      let overlap: number | null = null
      let turnChange: number | null = null
      if (anchorGray === null) {
        reason = '首帧建立覆盖基线'
      } else {
        overlap = translationCoverage(anchorGray, smallGray, smallCanvas.width, smallCanvas.height, 16, 8, 24)
        turnChange = bearingChange(telemetry, seconds)
        const pitchChange = anchorPitch !== null && position.gimbal_pitch_deg !== null
          ? Math.abs(position.gimbal_pitch_deg - anchorPitch) : null
        const turnActive = turnChange !== null && turnChange >= options.turnAngleDegrees
        const targetOverlap = turnActive ? options.turnOverlapPercent : options.straightOverlapPercent
        // 转弯/镜头变化不受最小间隔限制；直线覆盖至少间隔 1.5 秒，避免几乎相同的帧成对出现。
        const minGapSeconds = 1.5
        const tooClose = lastSelectedSeconds !== null && seconds - lastSelectedSeconds < minGapSeconds
        if (pitchChange !== null && pitchChange >= options.cameraChangeDegrees) reason = '镜头角度明显变化'
        else if (turnActive && (overlap === null || overlap <= targetOverlap)) reason = '转弯补覆盖'
        else if (tooClose) reason = ''
        else if (overlap === null) reason = '画面匹配不确定，保留以免漏区'
        else if (overlap <= targetOverlap) reason = '直线新增覆盖'
      }

      if (reason) {
        const candidate = { seconds, position, reason, overlap, turnChange, sharpnessRatio }
        candidates.push(candidate)
        pendingExports.push(candidate)
        lastSelectedSeconds = seconds
        anchorGray = smallGray
        anchorPitch = position.gimbal_pitch_deg
      } else if (anchorGray !== null) {
        // 未保留时把覆盖锚点向前链式推进（与后端一致）。
        anchorGray = smallGray
      }
      onProgress({ scanned, total, selected: candidates.length, blurred, blank, exported: selected.length })
      await yieldToMainThread()
    }

    scanDone = true
    await exportWorker
    return selected
  } finally {
    URL.revokeObjectURL(url)
  }
}

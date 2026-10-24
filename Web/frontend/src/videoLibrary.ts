// Client-side scan of a user-selected folder: DJI clips share one basename
// across MP4/SRT/LRF; group them for the library browser overlay. Everything
// here runs in the browser — the folder is never sent to the backend until a
// group is explicitly imported.

export type VideoLibraryGroup = {
  key: string          // 目录 + 小写 basename，用作分组键
  basename: string     // 原始 basename（不含扩展名），用于展示
  video: File
  srt: File | null
  lrf: File | null
  startAt: Date | null // 从文件名解析的录制开始时间（相机本地时间）
  endAt: Date | null   // 视频文件的修改时间
}

const VIDEO_EXTENSIONS = new Set(['mp4', 'mov', 'm4v'])
const PHOTO_EXTENSIONS = new Set(['jpg', 'jpeg', 'png', 'dng', 'arw', 'heic'])

// DJI clips are named DJI_20260120162828_0002_D.MP4: a 14-digit recording
// start stamp embedded between separators. Month/day are re-validated through
// a Date round-trip so filenames like 00000000000000 never parse.
const TIMESTAMP_IN_NAME = /(?:^|[_-])(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(?=[_.\s_-]|$)/

export function parseRecordingStart(basename: string): Date | null {
  const match = TIMESTAMP_IN_NAME.exec(basename)
  if (!match) return null
  const [, year, month, day, hour, minute, second] = match
  const date = new Date(Number(year), Number(month) - 1, Number(day), Number(hour), Number(minute), Number(second))
  const valid = date.getFullYear() === Number(year)
    && date.getMonth() === Number(month) - 1
    && date.getDate() === Number(day)
  return valid ? date : null
}

function relativePathOf(file: File): string {
  return file.webkitRelativePath || file.name
}

export function groupVideoFiles(files: File[]): VideoLibraryGroup[] {
  const byKey = new Map<string, VideoLibraryGroup>()
  for (const file of files) {
    const relative = relativePathOf(file)
    const parts = relative.split('/')
    const directory = parts.length > 1 ? parts.slice(0, -1).join('/') : ''
    const name = parts[parts.length - 1]
    const dot = name.lastIndexOf('.')
    if (dot <= 0) continue
    const basename = name.slice(0, dot)
    const extension = name.slice(dot + 1).toLowerCase()
    // 单独的照片文件属于图片，不进入视频库。
    if (PHOTO_EXTENSIONS.has(extension)) continue
    if (!VIDEO_EXTENSIONS.has(extension) && extension !== 'srt' && extension !== 'lrf') continue
    const key = `${directory}/${basename}`.toLowerCase()
    const existing = byKey.get(key)
    if (!existing) {
      if (!VIDEO_EXTENSIONS.has(extension)) continue // 没有 MP4 的 SRT/LRF 不构成视频组
      byKey.set(key, {
        key, basename, video: file, srt: null, lrf: null,
        startAt: parseRecordingStart(basename),
        endAt: new Date(file.lastModified),
      })
      continue
    }
    if (extension === 'srt' && !existing.srt) existing.srt = file
    else if (extension === 'lrf' && !existing.lrf) existing.lrf = file
  }
  return [...byKey.values()]
    .sort((a, b) => a.video.name.localeCompare(b.video.name, 'zh-Hans-CN', { numeric: true }))
}

// —— SRT 预统计：与后端 parse_srt_telemetry 同口径的轻量版，只数带有效 GPS 的字幕条数 ——
function cueHasGps(payload: string): boolean {
  const latitude = /(?:\[|\b)(?:latitude|lat)\s*[:=]\s*(-?\d+(?:\.\d+)?)/i.exec(payload)
  const longitude = /(?:\[|\b)(?:longitude|longtitude|lon|lng)\s*[:=]\s*(-?\d+(?:\.\d+)?)/i.exec(payload)
  if (latitude && longitude) {
    return inGpsRange(Number(latitude[1]), Number(longitude[1]))
  }
  const gps = /(?:\[|\b)gps(?:_info|_position)?\s*[:=]\s*\[?\s*(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)/i.exec(payload)
  return gps ? inGpsRange(Number(gps[1]), Number(gps[2])) : false
}

function inGpsRange(latitude: number, longitude: number): boolean {
  return latitude >= -90 && latitude <= 90 && longitude >= -180 && longitude <= 180
}

export async function countSrtTelemetry(file: File): Promise<number> {
  let text: string
  try {
    text = await file.text()
  } catch {
    return 0
  }
  let count = 0
  for (const block of text.replace(/\r\n/g, '\n').replace(/\ufeff/, '').split(/\n\s*\n/)) {
    const lines = block.split('\n')
    const timeIndex = lines.findIndex(line => line.includes('-->'))
    if (timeIndex < 0) continue
    if (cueHasGps(lines.slice(timeIndex + 1).join(' '))) count += 1
  }
  return count
}

// Capture a poster frame near the clip start for the library grid.
// DJI 主片可能是 HEVC，部分浏览器解不了；LRF 代理始终是 H.264，优先用它出封面。
export async function makeGroupThumbnail(group: Pick<VideoLibraryGroup, 'video' | 'lrf'>): Promise<string | null> {
  if (group.lrf) {
    const fromLrf = await makeVideoThumbnail(group.lrf)
    if (fromLrf) return fromLrf
  }
  return makeVideoThumbnail(group.video)
}

export function makeVideoThumbnail(file: File): Promise<string | null> {
  return new Promise(resolve => {
    const url = URL.createObjectURL(file)
    const video = document.createElement('video')
    let settled = false
    const finish = (value: string | null) => {
      if (settled) return
      settled = true
      video.removeAttribute('src')
      video.load()
      URL.revokeObjectURL(url)
      window.clearTimeout(timer)
      resolve(value)
    }
    const timer = window.setTimeout(() => finish(null), 8000)
    video.muted = true
    video.preload = 'metadata'
    video.addEventListener('loadedmetadata', () => {
      const duration = Number.isFinite(video.duration) ? video.duration : 1
      video.currentTime = Math.min(0.1, duration * 0.1)
    })
    video.addEventListener('seeked', () => {
      try {
        const width = 320
        const height = Math.max(1, Math.round(width * (video.videoHeight || 180) / (video.videoWidth || 320)))
        const canvas = document.createElement('canvas')
        canvas.width = width
        canvas.height = height
        const context = canvas.getContext('2d')
        if (!context) return finish(null)
        context.drawImage(video, 0, 0, width, height)
        finish(canvas.toDataURL('image/jpeg', 0.7))
      } catch {
        finish(null)
      }
    })
    video.addEventListener('error', () => finish(null))
    video.src = url
  })
}

function pad(value: number): string {
  return String(value).padStart(2, '0')
}

export function formatDateTime(date: Date | null): string {
  if (!date) return ''
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

export function formatClock(date: Date | null): string {
  if (!date) return ''
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

export function formatDurationBetween(start: Date | null, end: Date | null): string {
  if (!start || !end) return '未知'
  const seconds = Math.round((end.getTime() - start.getTime()) / 1000)
  if (seconds <= 0) return '结束早于开始（修改时间可能已被复制等操作改写）'
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
}

export function formatBytes(bytes = 0): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`
}

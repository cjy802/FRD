<!-- 任务列表的静态路线缩略图：天地图矢量瓦片 + 矢量路线。
     瓦片始终按原生 256px 1:1 贴图（不缩放），画布按 devicePixelRatio 放大后备分辨率，
     路线是矢量描线——小尺寸下瓦片和线条都不会糊。瓦片加载失败时退化为纯底色+路线。 -->
<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'

const props = defineProps<{
  coordinates: Array<[number, number]>
}>()

const WIDTH = 148
const HEIGHT = 104
const PADDING = 14
const TILE = 256

const canvasRef = ref<HTMLCanvasElement | null>(null)
const renderToken = ref(0)

// 列表里相邻任务大多在同一片区域，模块级瓦片缓存让第二个缩略图起零请求。
const tileCache = new Map<string, Promise<HTMLImageElement | null>>()

function loadTile(z: number, x: number, y: number): Promise<HTMLImageElement | null> {
  const key = `${z}/${x}/${y}`
  const cached = tileCache.get(key)
  if (cached) return cached
  const tk = import.meta.env.VITE_TIANDITU_TK?.trim()
  const promise = new Promise<HTMLImageElement | null>((resolve) => {
    if (!tk) {
      resolve(null)
      return
    }
    const image = new Image()
    image.onload = () => resolve(image)
    image.onerror = () => resolve(null)
    image.src = `https://t${Math.abs(x + y) % 8}.tianditu.gov.cn/DataServer?T=vec_w&x=${x}&y=${y}&l=${z}&tk=${tk}`
  })
  if (tileCache.size > 400) tileCache.clear()
  tileCache.set(key, promise)
  return promise
}

function lonToWorldX(longitude: number, z: number): number {
  return ((longitude + 180) / 360) * TILE * 2 ** z
}

function latToWorldY(latitude: number, z: number): number {
  const sin = Math.sin((latitude * Math.PI) / 180)
  return (0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI)) * TILE * 2 ** z
}

async function render(): Promise<void> {
  const canvas = canvasRef.value
  const coordinates = props.coordinates
  if (!canvas || coordinates.length < 2) return
  const token = ++renderToken.value
  const dpr = Math.min(window.devicePixelRatio || 1, 2)
  canvas.width = Math.round(WIDTH * dpr)
  canvas.height = Math.round(HEIGHT * dpr)
  const ctx = canvas.getContext('2d')
  if (!ctx) return
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  ctx.fillStyle = '#e6edf3'
  ctx.fillRect(0, 0, WIDTH, HEIGHT)

  let minLon = Infinity
  let maxLon = -Infinity
  let minLat = Infinity
  let maxLat = -Infinity
  for (const [longitude, latitude] of coordinates) {
    if (!Number.isFinite(longitude) || !Number.isFinite(latitude)) continue
    minLon = Math.min(minLon, longitude)
    maxLon = Math.max(maxLon, longitude)
    minLat = Math.min(minLat, latitude)
    maxLat = Math.max(maxLat, latitude)
  }
  if (!Number.isFinite(minLon) || !Number.isFinite(minLat)) return

  let zoom = 18
  while (zoom > 3) {
    const spanX = lonToWorldX(maxLon, zoom) - lonToWorldX(minLon, zoom)
    const spanY = latToWorldY(minLat, zoom) - latToWorldY(maxLat, zoom)
    if (spanX <= WIDTH - PADDING * 2 && spanY <= HEIGHT - PADDING * 2) break
    zoom -= 1
  }
  const centerX = (lonToWorldX(minLon, zoom) + lonToWorldX(maxLon, zoom)) / 2
  const centerY = (latToWorldY(minLat, zoom) + latToWorldY(maxLat, zoom)) / 2
  const originX = centerX - WIDTH / 2
  const originY = centerY - HEIGHT / 2

  const tileX0 = Math.floor(originX / TILE)
  const tileX1 = Math.floor((originX + WIDTH) / TILE)
  const tileY0 = Math.floor(originY / TILE)
  const tileY1 = Math.floor((originY + HEIGHT) / TILE)
  const requests: Array<{ image: Promise<HTMLImageElement | null>; x: number; y: number }> = []
  for (let ty = tileY0; ty <= tileY1; ty += 1) {
    for (let tx = tileX0; tx <= tileX1; tx += 1) {
      if (ty < 0 || ty >= 2 ** zoom) continue
      requests.push({ image: loadTile(zoom, tx, ty), x: tx, y: ty })
    }
  }
  const images = await Promise.all(requests.map((item) => item.image))
  if (token !== renderToken.value || !canvasRef.value) return
  for (let index = 0; index < requests.length; index += 1) {
    const image = images[index]
    if (!image) continue
    ctx.drawImage(image, requests[index].x * TILE - originX, requests[index].y * TILE - originY, TILE, TILE)
  }

  const points = coordinates
    .filter(([longitude, latitude]) => Number.isFinite(longitude) && Number.isFinite(latitude))
    .map(([longitude, latitude]) => [lonToWorldX(longitude, zoom) - originX, latToWorldY(latitude, zoom) - originY] as [number, number])
  if (points.length < 2) return
  ctx.lineJoin = 'round'
  ctx.lineCap = 'round'
  ctx.beginPath()
  ctx.moveTo(points[0][0], points[0][1])
  for (const [x, y] of points.slice(1)) ctx.lineTo(x, y)
  ctx.strokeStyle = '#ffffff'
  ctx.lineWidth = 4.5
  ctx.stroke()
  ctx.strokeStyle = '#1265a7'
  ctx.lineWidth = 2.5
  ctx.stroke()

  const drawEndpoint = (point: [number, number], color: string) => {
    ctx.beginPath()
    ctx.arc(point[0], point[1], 3.6, 0, Math.PI * 2)
    ctx.fillStyle = '#ffffff'
    ctx.fill()
    ctx.beginPath()
    ctx.arc(point[0], point[1], 2.6, 0, Math.PI * 2)
    ctx.fillStyle = color
    ctx.fill()
  }
  drawEndpoint(points[0], '#2e7d32')
  drawEndpoint(points[points.length - 1], '#a4262c')
}

onMounted(() => { void render() })
watch(() => props.coordinates, () => { void render() })
</script>

<template>
  <canvas
    ref="canvasRef"
    class="task-map-preview"
    role="img"
    aria-label="任务路线缩略图"
  ></canvas>
</template>

<style scoped>
.task-map-preview {
  display: block;
  flex: none;
  width: 148px;
  height: 104px;
  border: 1px solid var(--ui-line);
  border-radius: 6px;
  background: #e6edf3;
}
</style>

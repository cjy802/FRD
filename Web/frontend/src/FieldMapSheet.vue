<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { createBaiduScene, createMapScene, type BaiduScene } from './baiduScene'

export type FieldMapPoint = {
  id: string
  badge: string
  label: string
  detail: string
  latitude: number
  longitude: number
}

const props = defineProps<{ points: FieldMapPoint[]; selectedId: string | null }>()
const emit = defineEmits<{ close: []; select: [id: string]; complete: [id: string] }>()

const mapElement = ref<HTMLElement | null>(null)
const status = ref<'loading' | 'ready' | 'error'>('loading')
const errorMessage = ref('')
const markers = ref<Array<{ id: string; x: number; y: number }>>([])

let scene: BaiduScene | null = null
let stopFrame: (() => void) | null = null
let resizeObserver: ResizeObserver | null = null
let unmounted = false
let markerKey = ''

const selected = computed(() => props.points.find(point => point.id === props.selectedId) ?? null)

function center(): [number, number, number] {
  const lngs = props.points.map(point => point.longitude)
  const lats = props.points.map(point => point.latitude)
  return [(Math.min(...lngs) + Math.max(...lngs)) / 2, (Math.min(...lats) + Math.max(...lats)) / 2, 0]
}

function coordinates() {
  return props.points.map(point => [point.longitude, point.latitude, 2])
}

function syncMarkers() {
  if (!scene) return
  const rows: Array<{ id: string; x: number; y: number }> = []
  for (const point of props.points) {
    const pixel = scene.project([point.longitude, point.latitude, 2])
    if (pixel) rows.push({ id: point.id, x: Math.round(pixel.x), y: Math.round(pixel.y) })
  }
  const key = rows.map(row => `${row.id}:${row.x},${row.y}`).join('|')
  if (key !== markerKey) {
    markerKey = key
    markers.value = rows
  }
}

function focusPoint(point: FieldMapPoint) {
  scene?.focus([point.longitude, point.latitude, 2], '2d')
}

function fitAll() {
  scene?.fit(coordinates(), '2d')
}

watch(() => props.selectedId, id => {
  const point = props.points.find(item => item.id === id)
  if (point) focusPoint(point)
})

onMounted(async () => {
  const element = mapElement.value
  if (!element) return
  if (!props.points.length) {
    status.value = 'error'
    errorMessage.value = '没有可显示的坐标。'
    return
  }
  // 百度底图不可用（缺 AK、断网、资源加载失败）时退回 OSM 底图，保证现场端还能看位置。
  try {
    try {
      scene = await createBaiduScene(element, center())
    } catch {
      scene = await createMapScene(element, center(), 'osm')
    }
    if (unmounted) { scene.dispose(); scene = null; return }
    stopFrame = scene.onFrame(syncMarkers)
    scene.setView('2d', coordinates())
    fitAll()
    if (selected.value) focusPoint(selected.value)
    status.value = 'ready'
    resizeObserver = new ResizeObserver(() => scene?.resize())
    resizeObserver.observe(element)
    // setProjection 异步重建地图时会丢掉同帧设置的相机位置，稳定后重取一次景；
    // 用户已经自己动手拖动过就不再打扰。
    let interacted = false
    const markInteracted = () => { interacted = true }
    element.addEventListener('pointerdown', markInteracted, { once: true, capture: true })
    window.setTimeout(() => {
      element.removeEventListener('pointerdown', markInteracted, { capture: true })
      if (unmounted || interacted || !scene) return
      fitAll()
      if (selected.value) focusPoint(selected.value)
    }, 700)
  } catch (cause) {
    scene?.dispose()
    scene = null
    if (!unmounted) {
      status.value = 'error'
      errorMessage.value = cause instanceof Error ? cause.message : '地图加载失败，请稍后重试。'
    }
  }
})

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') emit('close')
}

onMounted(() => window.addEventListener('keydown', onKeydown))

onBeforeUnmount(() => {
  unmounted = true
  window.removeEventListener('keydown', onKeydown)
  stopFrame?.()
  stopFrame = null
  resizeObserver?.disconnect()
  resizeObserver = null
  scene?.dispose()
  scene = null
})
</script>

<template>
  <Teleport to="body">
    <div class="field-map-sheet" role="dialog" aria-modal="true" aria-label="积水点位地图">
      <header class="field-map-head">
        <div>
          <strong>积水点位地图</strong>
          <span>共 {{ points.length }} 处待处理点位 · 点击节点查看</span>
        </div>
        <button type="button" class="field-map-close" aria-label="关闭地图" @click="emit('close')">×</button>
      </header>
      <div class="field-map-body">
        <div ref="mapElement" class="field-map-canvas"></div>
        <div v-if="status === 'ready'" class="field-map-markers">
          <button v-for="marker in markers" :key="marker.id" type="button" class="field-map-marker" :class="{ selected: marker.id === selectedId }" :style="{ left: `${marker.x}px`, top: `${marker.y}px` }" :aria-label="`定位到 ${points.find(point => point.id === marker.id)?.label ?? '积水点'}`" @click="emit('select', marker.id)">{{ points.find(point => point.id === marker.id)?.badge }}</button>
        </div>
        <div v-if="status === 'loading'" class="field-map-state" role="status"><span class="field-loader"></span><strong>正在加载地图</strong><small>加载失败时会提示，坐标信息始终显示在下方。</small></div>
        <div v-else-if="status === 'error'" class="field-map-state" role="alert"><strong>地图暂时加载不了</strong><small>{{ errorMessage }}</small></div>
      </div>
      <footer v-if="selected" class="field-map-foot">
        <div>
          <strong>{{ selected.label }}</strong>
          <span>{{ selected.detail }}</span>
        </div>
        <div class="field-map-actions">
          <button type="button" @click="fitAll">显示全部</button>
          <button type="button" class="is-primary" @click="emit('complete', selected.id)">标记为已处理</button>
        </div>
      </footer>
    </div>
  </Teleport>
</template>

<style scoped>
.field-map-sheet { position: fixed; inset: 0; z-index: 60; display: flex; flex-direction: column; background: #f3f5f7; }
.field-map-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex: none; min-height: 60px; padding: 10px 16px; background: #fff; border-bottom: 1px solid var(--ui-line); }
.field-map-head strong { display: block; color: #111827; font-size: var(--font-heading); font-weight: 600; letter-spacing: -.02em; }
.field-map-head span { display: block; margin-top: 1px; color: var(--ui-muted); font-size: var(--font-caption); }
.field-map-close { display: grid; place-items: center; flex: none; width: 36px; height: 36px; padding: 0; border: 1px solid #d7dfe8; border-radius: 50%; background: #fff; color: #34506c; font-size: 22px; line-height: 1; }
.field-map-close:hover { border-color: var(--ui-accent); background: #eff6ff; color: var(--ui-accent); }
.field-map-body { position: relative; flex: 1; min-height: 0; overflow: hidden; background: #eaf0f6; }
.field-map-canvas { position: absolute; inset: 0; }
.field-map-markers { position: absolute; inset: 0; overflow: hidden; pointer-events: none; }
.field-map-marker { position: absolute; display: grid; place-items: center; width: 28px; height: 28px; padding: 0; border: 2px solid #fff; border-radius: 999px; background: #d0342c; color: #fff; font-size: 11px; font-weight: 700; transform: translate(-50%, -100%); box-shadow: 0 2px 8px rgb(15 23 42 / .3); pointer-events: auto; }
.field-map-marker::after { content: ''; position: absolute; bottom: -5px; left: 50%; width: 8px; height: 8px; border-right: 2px solid #fff; border-bottom: 2px solid #fff; background: inherit; transform: translateX(-50%) rotate(45deg); }
.field-map-marker.selected { z-index: 1; width: 34px; height: 34px; border-color: #fff; background: var(--ui-accent); font-size: 13px; }
.field-map-state { position: absolute; top: 50%; left: 50%; display: grid; justify-items: center; gap: 8px; padding: 26px 30px; border: 1px solid #e1e6eb; border-radius: var(--ui-radius); background: #fffffff2; box-shadow: 0 12px 36px rgb(15 23 42 / .2); color: var(--ui-muted); text-align: center; transform: translate(-50%, -50%); }
.field-map-state strong { color: #242424; font-size: var(--font-heading); font-weight: 600; }
.field-map-state small { max-width: 260px; font-size: var(--font-caption); line-height: 1.6; }
.field-loader { width: 30px; height: 30px; border: 3px solid #c7ddf1; border-top-color: var(--ui-accent); border-radius: 50%; animation: field-map-spin .8s linear infinite; }
@keyframes field-map-spin { to { transform: rotate(360deg); } }
.field-map-foot { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex: none; padding: 10px 16px; background: #fff; border-top: 1px solid var(--ui-line); }
.field-map-foot strong { display: block; overflow: hidden; color: #1e3a5f; font-size: var(--font-body); font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }
.field-map-foot span { display: block; margin-top: 1px; color: var(--ui-muted); font-size: var(--font-caption); font-variant-numeric: tabular-nums; }
.field-map-actions { display: flex; flex: none; gap: 8px; }
.field-map-actions button { min-height: 34px; padding: 5px 12px; border: 1px solid #d7dfe8; border-radius: 7px; background: #fff; color: #34506c; font-size: var(--font-caption); white-space: nowrap; }
.field-map-actions button:hover:not(:disabled) { border-color: var(--ui-accent); background: #eff6ff; color: var(--ui-accent); }
.field-map-actions .is-primary { border-color: var(--ui-accent); background: var(--ui-accent); color: #fff; font-weight: 600; }
.field-map-actions .is-primary:hover:not(:disabled) { border-color: var(--ui-accent-hover); background: var(--ui-accent-hover); color: #fff; }
@media (max-width: 460px) {
  .field-map-foot { align-items: stretch; flex-direction: column; gap: 8px; }
  .field-map-actions button { flex: 1; }
}
</style>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { createBaiduScene, type BaiduScene, type CameraPose } from './baiduScene'
const props = defineProps<{ active: boolean; pose: CameraPose | null }>()
const element = ref<HTMLElement | null>(null)
const status = ref('正在加载百度视角…')
let scene: BaiduScene | null = null
let observer: ResizeObserver | null = null
let unmounted = false
function refresh() { if (props.pose) scene?.setPose(props.pose) }
onMounted(async () => {
  try {
    const pose = props.pose
    const nextScene = await createBaiduScene(element.value!, pose ? [pose.longitude, pose.latitude, pose.altitude] : [119.95, 31.68, 100], true)
    if (unmounted) { nextScene.dispose(); return }
    scene = nextScene
    refresh(); scene.setActive(props.active)
    observer = new ResizeObserver(() => { scene?.resize(); refresh() })
    observer.observe(element.value!)
    status.value = ''
  } catch (error) { scene?.dispose(); scene = null; status.value = error instanceof Error ? error.message : '百度视角加载失败。' }
})
watch(() => props.pose, refresh, { deep: true, flush: 'sync' })
watch(() => props.active, async active => { await nextTick(); scene?.setActive(active); if (active) { scene?.resize(); refresh() } })
onBeforeUnmount(() => { unmounted = true; observer?.disconnect(); scene?.dispose() })
</script>
<template><div class="baidu-camera-preview"><div ref="element" class="baidu-camera-canvas"></div><span v-if="status" class="baidu-camera-status" role="status">{{ status }}</span><span class="baidu-camera-credit">© 百度地图</span></div></template>
<style scoped>
.baidu-camera-preview, .baidu-camera-canvas { position: absolute; inset: 0; }
.baidu-camera-status { position: absolute; top: 50%; left: 0; right: 0; padding: 8px; background: #ffffffdf; text-align: center; font-size: 12px; }
.baidu-camera-credit { position: absolute; right: 5px; bottom: 28px; color: white; text-shadow: 0 1px 2px black; font-size: 12px; }
</style>

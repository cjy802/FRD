import { createMapScene } from './baiduScene'

const container = document.getElementById('map')
const statusElement = document.getElementById('status')
if (!container || !statusElement) {
  throw new Error('底图预览容器不存在。')
}

try {
  // 演示视频取景地(常州大学校区),默认 3D 视角与工作台一致。
  const scene = await createMapScene(container, [119.9505, 31.6798, 0], 'osm')
  scene.setView('3d', [[119.9505, 31.6798, 0]])
  const source = import.meta.env.VITE_TIANDITU_TK?.trim()
    ? '天地图矢量底图(无注记)'
    : '标准 OSM 瓦片(带地名;未配置 VITE_TIANDITU_TK)'
  statusElement.textContent = `当前底图:${source}`
  window.addEventListener('beforeunload', () => scene.dispose(), { once: true })
  import.meta.hot?.dispose(() => scene.dispose())
} catch (error) {
  statusElement.textContent = error instanceof Error ? error.message : String(error)
}

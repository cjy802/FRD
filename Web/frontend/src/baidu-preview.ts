export {}

const container = document.getElementById('map')
const statusElement = document.getElementById('status')
const ak = import.meta.env.VITE_BAIDU_MAP_AK?.trim() ?? ''

if (!container || !statusElement) {
  throw new Error('百度地图预览容器不存在。')
}

if (!ak) {
  statusElement.textContent = '尚未填写 AK：请在 frontend/.env.local 中填写 VITE_BAIDU_MAP_AK，然后重启前端。'
} else {
  try {
    const mapvthree = await import('@baidumap/mapv-three')
    mapvthree.BaiduMapConfig.ak = ak
    const engine = new mapvthree.Engine(container, {
      map: {
        provider: new mapvthree.BaiduVectorTileProvider({
          ak,
          displayOptions: { poi: true },
        }),
        center: [116.404, 39.915],
        heading: 0,
        pitch: 45,
        range: 900,
      },
      rendering: { enableAnimationLoop: true },
    } as any)
    statusElement.textContent = '引擎已初始化。若底图未显示，请检查 AK 的浏览器端配置。'
    window.addEventListener('beforeunload', () => engine.dispose(), { once: true })
    import.meta.hot?.dispose(() => engine.dispose())
  } catch (error) {
    statusElement.textContent = error instanceof Error ? `初始化失败：${error.message}` : '地图初始化失败。'
  }
}

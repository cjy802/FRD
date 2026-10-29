import { defineConfig, loadEnv, type Plugin } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath } from 'node:url'

const frontendRoot = fileURLToPath(new URL('.', import.meta.url))

// mapv-three 1.7.2 的 Cesium 移植层把 Cartesian2/Matrix3/Matrix4 的
// static clone(t, e) 写成了无条件 e.copy(t)。触摸路径
// （ScreenSpaceEventHandler.handlePointerDown）调用 clone 时只传一个
// 参数，在手机上每次触摸都抛 TypeError。给 clone 补上 result 缺省分支。
const brokenClone = 'return e.copy(t), e;'
const patchedClone = 'return e ? (e.copy(t), e) : new t.constructor().copy(t);'
const patchMapvThreeTouchClone: Plugin = {
  name: 'patch-mapv-three-touch-clone',
  transform(code, id) {
    if (!id.includes('mapvthree') || !code.includes(brokenClone)) return null
    return { code: code.split(brokenClone).join(patchedClone), map: null }
  },
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, frontendRoot, 'VITE_')
  const apiTarget = process.env.VITE_API_TARGET || env.VITE_API_TARGET || 'http://127.0.0.1:8011'
  const proxy = { '/api': apiTarget }

  return {
    // 补丁依赖 transform 管线；预打包（esbuild）会绕过它，所以排除该依赖。
    optimizeDeps: { exclude: ['@baidumap/mapv-three'] },
    plugins: [
      vue({ template: { compilerOptions: { isCustomElement: (tag) => tag.startsWith('fluent-') } } }),
      patchMapvThreeTouchClone,
    ],
    server: { proxy },
    preview: { proxy },
    build: {
      rollupOptions: {
        input: fileURLToPath(new URL('./index.html', import.meta.url)),
      },
    },
  }
})

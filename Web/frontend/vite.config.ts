import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath } from 'node:url'
import { resolve } from 'node:path'

const frontendRoot = fileURLToPath(new URL('.', import.meta.url))

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, frontendRoot, 'VITE_')
  const apiTarget = process.env.VITE_API_TARGET || env.VITE_API_TARGET || 'http://127.0.0.1:8011'
  const proxy = { '/api': apiTarget }

  return {
    plugins: [vue({ template: { compilerOptions: { isCustomElement: (tag) => tag.startsWith('fluent-') } } })],
    server: { proxy },
    preview: { proxy },
    build: {
      rollupOptions: {
        input: {
          main: resolve(frontendRoot, 'index.html'),
          baiduPreview: resolve(frontendRoot, 'baidu-preview.html'),
        },
      },
    },
  }
})

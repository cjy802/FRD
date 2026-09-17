import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath } from 'node:url'
import { resolve } from 'node:path'

const frontendRoot = fileURLToPath(new URL('.', import.meta.url))

export default defineConfig({
  plugins: [vue({ template: { compilerOptions: { isCustomElement: (tag) => tag.startsWith('fluent-') } } })],
  server: { proxy: { '/api': process.env.VITE_API_TARGET || 'http://127.0.0.1:8000' } },
  build: {
    rollupOptions: {
      input: {
        main: resolve(frontendRoot, 'index.html'),
        baiduPreview: resolve(frontendRoot, 'baidu-preview.html'),
      },
    },
  },
})

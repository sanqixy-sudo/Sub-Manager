import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  build: { outDir: '../app/static', emptyOutDir: true, rollupOptions:{output:{manualChunks:{vue:['vue','vue-router','pinia'],element:['element-plus']}}} },
  server: { proxy: { '/api': 'http://127.0.0.1:7777', '/s': 'http://127.0.0.1:7777' } },
})

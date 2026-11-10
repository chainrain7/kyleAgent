import path from 'node:path'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    // @ 别名 → src(shadcn/ui 组件与内部导入统一走它)
    alias: {
      '@': path.resolve(import.meta.dirname, './src'),
    },
  },
  build: {
    // 产物直接落到桥接器的静态目录 webui/dist(提交入库,CI 零 Node)
    outDir: '../dist',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      // WS 中继:与 kyle-webui(127.0.0.1:8080)同源转发
      '/ws': {
        target: 'ws://127.0.0.1:8080',
        ws: true,
      },
      '/api': {
        target: 'http://127.0.0.1:8080',
      },
    },
  },
})

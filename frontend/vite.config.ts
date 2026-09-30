import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The backend serves the built app from frontend/dist; in development Vite proxies /api.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { '/api': { target: 'http://127.0.0.1:8765', changeOrigin: false } },
  },
})

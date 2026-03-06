import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    proxy: {
      '/ollama': { target: 'http://localhost:11434', rewrite: p => p.replace(/^\/ollama/, ''), changeOrigin: true },
      '/api':    { target: 'http://localhost:8080',  changeOrigin: true },
    }
  }
})

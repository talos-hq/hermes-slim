import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Served by the gateway listener under /talos/, so every asset URL is
// absolute from that root and the app works from the install link's path.
export default defineConfig({
  base: '/talos/',
  plugins: [react()],
  build: { outDir: 'dist', emptyOutDir: true },
  test: { environment: 'jsdom' },
})

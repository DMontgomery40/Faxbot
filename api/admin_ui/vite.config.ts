/// <reference types="vitest" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const api = 'http://localhost:8080'

export default defineConfig({
  plugins: [react()],
  base: '/admin/ui/',
  css: {
    // Prevent PostCSS from walking up outside the project (avoids permission errors)
    postcss: {}
  },
  build: {
    outDir: 'dist',
    assetsDir: 'assets',
    sourcemap: false,
    minify: 'esbuild'
  },
  server: {
    port: 3000,
    strictPort: true,
    proxy: {
      // Everything under /admin except the dev server's own /admin/ui/ pages,
      // including the terminal WebSocket.
      '^/admin/(?!ui(?:/|$))': { target: api, ws: true },
      '/auth': api,
      '/access': api,
      '/fax': api,
      '/inbound': api,
      '/plugins': api,
      '/plugin-registry': api,
      '/health': api,
      '/mobile': api
    }
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    testTimeout: 20000
  }
})

import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  base: process.env.ELECTRON ? './' : '/admin/ui/',
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
    port: 5173, // Changed to match Electron expectation
    proxy: {
      '/admin': 'http://localhost:8080',
      '/fax': 'http://localhost:8080',
      '/inbound': 'http://localhost:8080'
    }
  }
})

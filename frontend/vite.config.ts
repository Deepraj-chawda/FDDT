import path from 'node:path'

import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig, type Plugin } from 'vite'

import { APP_TITLE } from './src/lib/appInfo.ts'

// Fills the `%APP_TITLE%` placeholder in index.html from the single
// product-name constant (src/lib/appInfo.ts) instead of hardcoding it there.
const appTitle = (): Plugin => ({
  name: 'app-title',
  transformIndexHtml: (html) => html.replaceAll('%APP_TITLE%', APP_TITLE),
})

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss(), appTitle()],
  resolve: {
    alias: {
      '@': path.resolve(import.meta.dirname, './src'),
    },
  },
  server: {
    proxy: {
      // Backend runs on :8000 (see backend/README / CLAUDE.md). Proxying
      // avoids CORS config for local dev.
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ''),
      },
    },
  },
})

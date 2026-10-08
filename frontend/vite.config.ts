import fs from 'node:fs'
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

// pdf.js's decoders and data (src/lib/pdfjs.ts PDF_DOCUMENT_OPTIONS) served at
// /pdfjs/<dir>/ straight from node_modules/pdfjs-dist in dev, and copied into
// the build — so they always match the installed pdf.js version.
const PDFJS_DIRS = ['wasm', 'cmaps', 'standard_fonts', 'iccs']
const pdfjsRoot = path.resolve(import.meta.dirname, 'node_modules/pdfjs-dist')
const pdfjsAssets = (): Plugin => ({
  name: 'pdfjs-assets',
  configureServer(server) {
    server.middlewares.use('/pdfjs', (req, res, next) => {
      const rel = decodeURIComponent((req.url ?? '').split('?')[0]).replace(/^\/+/, '')
      const file = path.resolve(pdfjsRoot, rel)
      const [dir] = rel.split('/')
      if (!PDFJS_DIRS.includes(dir) || !file.startsWith(pdfjsRoot + path.sep) || !fs.existsSync(file)) {
        return next()
      }
      if (file.endsWith('.wasm')) res.setHeader('Content-Type', 'application/wasm')
      fs.createReadStream(file).pipe(res)
    })
  },
  generateBundle() {
    for (const dir of PDFJS_DIRS) {
      for (const name of fs.readdirSync(path.join(pdfjsRoot, dir))) {
        this.emitFile({
          type: 'asset',
          fileName: `pdfjs/${dir}/${name}`,
          source: fs.readFileSync(path.join(pdfjsRoot, dir, name)),
        })
      }
    }
  },
})

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss(), appTitle(), pdfjsAssets()],
  resolve: {
    alias: {
      '@': path.resolve(import.meta.dirname, './src'),
    },
    dedupe: ['react', 'react-dom'],
  },
  optimizeDeps: {
    include: [
      'react',
      'react-dom',
      'react-dom/client',
      'react-router-dom',
      '@tanstack/react-query',
    ],
  },
  server: {
    allowedHosts: [
      'busybody-entail-parkway.ngrok-free.dev',
      '.ngrok-free.dev',
      '.ngrok.app',
    ],
    proxy: {
      // Backend runs on :8000 (see backend/README / SPECIFICATION.md). Proxying
      // avoids CORS config for local dev.
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ''),
      },
    },
  },
})

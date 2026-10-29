import { defineConfig, mergeConfig } from 'vitest/config'

import viteConfig from './vite.config.ts'

// The app's Vite config (aliases, plugins) plus a browser-like DOM for
// component tests. Tests live next to the code: src/**/*.test.ts(x).
export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      environment: 'jsdom',
      include: ['src/**/*.test.{ts,tsx}'],
      setupFiles: ['./src/test/setup.ts'],
    },
  }),
)

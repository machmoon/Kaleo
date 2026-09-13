import { svelte } from '@sveltejs/vite-plugin-svelte'
import { defineConfig } from 'vite'

// The bundle is served by service/app.py from the same origin, so every asset
// URL must be relative and no CORS is involved anywhere.
const API = process.env.SILKSCREEN_API || 'http://127.0.0.1:8081'

export default defineConfig({
  plugins: [svelte()],
  base: './',
  build: { sourcemap: true },
  server: {
    proxy: {
      // Literal 127.0.0.1, never `localhost`: Node >=17 resolves `localhost`
      // to ::1 while the Python service binds IPv4 only.
      '/generate': {
        target: API,
        changeOrigin: false,
        // A solve can legitimately run for minutes; the default proxy timeout
        // would sever the request mid-run.
        timeout: 300000,
        proxyTimeout: 300000,
      },
      '/chat': {
        target: API,
        changeOrigin: false,
        timeout: 300000,
        proxyTimeout: 300000,
      },
      '/models': { target: API, changeOrigin: false },
      '/config': { target: API, changeOrigin: false },
      '/healthz': { target: API, changeOrigin: false },
    },
  },
})

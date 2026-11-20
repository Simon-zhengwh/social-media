import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv } from 'vite'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const defaultTarget = process.env.SOCIAL_MEDIA_API_PROXY_TARGET || 'http://127.0.0.1:8000'
  const localProxy = loadEnv(mode, process.cwd(), 'SOCIAL_MEDIA_API_PROXY_TARGET_')
  // An ignored local env file can rebind a running dev proxy without changing other upstreams.
  const target = localProxy[`SOCIAL_MEDIA_API_PROXY_TARGET_${new URL(defaultTarget).port}`] || defaultTarget
  return {
    plugins: [react()],
    server: {
      host: '127.0.0.1',
      proxy: {
        '/api': {
          target,
          changeOrigin: true,
        },
      },
    },
  }
})

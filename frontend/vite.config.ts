import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Dev proxy: `npm run dev` serves the app with hot reload while the API
// endpoints are forwarded to the local stack (make up) on :8000.
// Production never sees this — the built bundle is served by FastAPI itself.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/stats': 'http://localhost:8000',
      '/recent': 'http://localhost:8000',
      '/ws/claims': { target: 'http://localhost:8000', ws: true },
    },
  },
})

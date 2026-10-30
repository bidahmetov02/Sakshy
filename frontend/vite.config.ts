import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // Host dev: the API runs on the host (`uv run uvicorn ...`) or in compose with port 8000 exposed.
    proxy: { '/api': 'http://localhost:8000' },
  },
})

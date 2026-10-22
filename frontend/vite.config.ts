import path from "node:path"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vitest/config"

// In development the browser talks to the Vite server, which forwards /api to
// the FastAPI layer: the page and the API share one origin, so the session
// cookie (HttpOnly, SameSite=Strict) works without any CORS exception.
export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": path.resolve(__dirname, "src") } },
  server: {
    port: 5173,
    proxy: { "/api": { target: process.env.LOGIDOC_API_URL ?? "http://127.0.0.1:8000", changeOrigin: false } },
  },
  build: { outDir: "dist", sourcemap: false, chunkSizeWarningLimit: 900 },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: "./src/test/setup.ts",
    css: false,
    restoreMocks: true,
  },
})

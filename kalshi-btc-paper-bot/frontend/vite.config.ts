import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Node's process global (vite.config.ts runs in Node); declared to avoid a @types/node dependency.
declare const process: { env: Record<string, string | undefined> };

// Dev server (start.ps1 -Dev) runs on localhost only and proxies the API to the backend.
// The normal start serves the built dashboard from the backend at http://127.0.0.1:8000.
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    // start.ps1 exports KBOT_PORT (from -Port or .env) before launching the dev server.
    proxy: { "/api": `http://127.0.0.1:${process.env.KBOT_PORT || 8000}` },
  },
  preview: { host: "127.0.0.1", port: 4173 },
  build: { outDir: "dist", sourcemap: false, chunkSizeWarningLimit: 1200 },
});

import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run build` writes into the Python package so `la ui` can serve it without Node.
// For UI development, run `la ui --browser --port 8765` and then `npm run dev`.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "../src/localagent/server/static",
    emptyOutDir: true,
    // Monaco is one ~3.4 MB chunk of its own, loaded the first time a file or diff tab opens.
    chunkSizeWarningLimit: 3600,
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8765",
      "/ws": { target: "ws://127.0.0.1:8765", ws: true },
    },
  },
});

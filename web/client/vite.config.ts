import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Use relative paths so the build works at any sub-path without baking
// the path into the bundle. The server injects <base href> + window.__BASE_PATH__
// at runtime from the BASE_PATH environment variable.
export default defineConfig({
  plugins: [react()],
  base: "./",
  server: {
    proxy: {
      "/api": "http://localhost:8000",
      "/ws": { target: "ws://localhost:8000", ws: true },
    },
  },
});

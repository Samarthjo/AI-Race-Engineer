import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The built UI is served by `pitwall serve` from the Python package.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "../src/pitwall/api/static",
    emptyOutDir: true,
    chunkSizeWarningLimit: 900,
  },
  server: {
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
});

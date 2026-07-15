import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react],
  server: {
    proxy: {
      "/config": "http://127.0.0.1:8000",
      "/benchmarks": "http://127.0.0.1:8000",
      "/events": "http://127.0.0.1:8000",
      "/health": "http://127.0.0.1:8000",
      "/investigations": "http://127.0.0.1:8000",
    },
  },
});

import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Dev proxy: the browser talks same-origin to Vite, so backend CORS stays untouched.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: { "/api": { target: process.env.RFQ_API_TARGET ?? "http://127.0.0.1:8000", changeOrigin: true } },
  },
  // Offline unit tests: jsdom, mocked fetch, no server or database.
  test: { environment: "jsdom", setupFiles: ["./src/test/setup.ts"] },
});

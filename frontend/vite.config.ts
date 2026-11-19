import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { DECISION_PROXY, ORDINARY_PROXY, decisionTarget } from "./decisionProxy";

// Dev proxy: the browser talks same-origin to Vite, so backend CORS stays untouched.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      [DECISION_PROXY]: { target: decisionTarget(process.env.RFQ_DECISION_TARGET), changeOrigin: true },
      [ORDINARY_PROXY]: { target: process.env.RFQ_API_TARGET ?? "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
  // Offline unit tests: jsdom, mocked fetch, no server or database.
  test: { environment: "jsdom", setupFiles: ["./src/test/setup.ts"] },
});

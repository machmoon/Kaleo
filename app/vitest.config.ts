import { defineConfig } from "vitest/config";
import path from "path";

// Separate from vite.config.ts on purpose: the app config is tuned for Tauri
// (fixed port, tailwind plugin) and none of that belongs in a test run.
// A future component test will need `environment: "jsdom"` and jsdom installed.
export default defineConfig({
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.{ts,tsx}"],
  },
});

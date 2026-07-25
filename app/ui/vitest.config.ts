import { readFileSync } from "node:fs";
import { defineConfig } from "vitest/config";

const { version } = JSON.parse(readFileSync("./../src-tauri/tauri.conf.json", "utf-8"));

export default defineConfig({
  define: { __APP_VERSION__: JSON.stringify(version) },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
    include: ["src/**/*.test.tsx", "src/**/*.test.ts"],
  },
});

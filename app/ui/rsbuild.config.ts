import { readFileSync } from "node:fs";
import { defineConfig } from "@rsbuild/core";
import { pluginReact } from "@rsbuild/plugin-react";

// Single version source: tauri.conf.json (issue #7)
const { version } = JSON.parse(readFileSync("../src-tauri/tauri.conf.json", "utf-8"));

export default defineConfig({
  plugins: [pluginReact()],
  source: { define: { __APP_VERSION__: JSON.stringify(version) } },
  html: { title: "Filmpaw" },
  server: { port: 3000, strictPort: true },
  dev: { assetPrefix: "/" },
  output: { assetPrefix: "./" },
});

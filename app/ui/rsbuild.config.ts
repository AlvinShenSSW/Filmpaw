import { readFileSync } from "node:fs";
import { defineConfig } from "@rsbuild/core";
import { pluginReact } from "@rsbuild/plugin-react";

// Single version source: tauri.conf.json (issue #7)
const { version } = JSON.parse(readFileSync("../src-tauri/tauri.conf.json", "utf-8"));

export default defineConfig(({ env, command }) => ({
  plugins: [pluginReact()],
  source: {
    define: {
      __APP_VERSION__: JSON.stringify(version),
      // A production bundle must never contain a parent shell's credential.
      // Tauri injects a fresh value at runtime; standalone development shares
      // this explicitly supplied value with its temporary sidecar.
      __FILMPAW_DEV_TOKEN__: JSON.stringify(
        env === "development" && command === "dev" ? process.env.FILMPAW_SESSION_TOKEN ?? "" : "",
      ),
    },
  },
  html: { title: "Filmpaw" },
  server: { port: 3000, strictPort: true },
  dev: { assetPrefix: "/" },
  output: { assetPrefix: "./" },
}));

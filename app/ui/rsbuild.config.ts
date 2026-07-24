import { defineConfig } from "@rsbuild/core";
import { pluginReact } from "@rsbuild/plugin-react";

export default defineConfig({
  plugins: [pluginReact()],
  html: { title: "Filmpaw" },
  server: { port: 3000, strictPort: true },
  dev: { assetPrefix: "/" },
  output: { assetPrefix: "./" },
});

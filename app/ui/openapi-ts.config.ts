import { defineConfig } from "@hey-api/openapi-ts";

export default defineConfig({
  input: "./openapi.json",
  output: { path: "./src/client", format: "biome" },
  plugins: ["@hey-api/client-fetch"],
});

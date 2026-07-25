/** Server port resolution: Tauri injects window.__FILMPAW_PORT__; dev falls back to 8720. */

declare global {
  const __APP_VERSION__: string;
  interface Window {
    __FILMPAW_PORT__?: number;
    __TAURI_INTERNALS__?: unknown;
  }
}

export function serverBase(): string {
  // 8720 is the documented standalone-dev convention (see ui/README.md:
  // `uv run filmpaw-server --port 8720`); inside Tauri the injected
  // OS-assigned port always wins.
  const port = window.__FILMPAW_PORT__ ?? 8720;
  return `http://127.0.0.1:${port}`;
}

import { client } from "./client/client.gen";

/** Point the generated SDK at the sidecar. Call once at startup. */
export function configureClient(): void {
  client.setConfig({ baseUrl: serverBase() });
}

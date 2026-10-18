/** Server port resolution: Tauri injects window.__FILMPAW_PORT__; dev falls back to 8720. */

declare global {
  const __APP_VERSION__: string;
  const __FILMPAW_DEV_TOKEN__: string;
  interface Window {
    __FILMPAW_PORT__?: number;
    __FILMPAW_SESSION_TOKEN__?: string;
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

/** One session credential for both the SDK and archive fetches. */
export function sessionHeaders(): Record<string, string> {
  const devToken = typeof __FILMPAW_DEV_TOKEN__ === "string" ? __FILMPAW_DEV_TOKEN__ : "";
  const token = window.__FILMPAW_SESSION_TOKEN__ ?? devToken;
  return /^[a-f0-9]{64}$/i.test(token) ? { "X-Filmpaw-Session": token } : {};
}

/** Point the generated SDK at the sidecar. Call once at startup. */
export function configureClient(): void {
  client.setConfig({ baseUrl: serverBase(), headers: sessionHeaders() });
}

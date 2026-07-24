/** Server port resolution: Tauri injects window.__FILMPAW_PORT__; dev falls back to 8720. */

declare global {
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

export interface Health {
  status: string;
  version: string;
}

export async function fetchHealth(): Promise<Health> {
  const resp = await fetch(`${serverBase()}/api/health`);
  if (!resp.ok) throw new Error(`health ${resp.status}`);
  return resp.json() as Promise<Health>;
}

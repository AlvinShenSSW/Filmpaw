/** Shared native directory picker used by the archive and settings pages.
 * Tauri opens the OS dialog (browses network locations too); the dev build
 * without Tauri falls back to a prompt. Returns null on cancel/empty. */
export async function pickDirectory(title: string): Promise<string | null> {
  const { isTauri } = await import("@tauri-apps/api/core");
  if (isTauri()) {
    const { open } = await import("@tauri-apps/plugin-dialog");
    const r = await open({ directory: true, title });
    return typeof r === "string" ? r : null;
  }
  const r = window.prompt(`${title} (dev 模式: 输入路径)`);
  return r?.trim() ? r.trim() : null;
}

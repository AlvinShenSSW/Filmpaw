/** Single place that decides WHO opens a folder (#31).
 *
 * Windows only lets the foreground process (or one it started) activate a
 * window. In the packaged app the click lands on the Tauri shell, so the shell
 * must be the one to call ShellExecuteW — when the sidecar did it, Explorer
 * opened behind the app and only blinked in the taskbar.
 *
 * The server stays the validation authority in BOTH branches: the shell command
 * takes the same intent parameters the HTTP call takes (an id, a subdir name —
 * never a path) and opens only what the server resolved.
 *
 * Both pages go through here so the two branches cannot drift apart (#19).
 * Failures reject with the server's `{status, detail}`, which is what the pages'
 * `detailOf(e)` already reads.
 */
import { openPairApiOpenPairPost, openPerformerApiPerformersPerformerIdOpenPost } from "./client";

async function inTauri(): Promise<boolean> {
  try {
    const { isTauri } = await import("@tauri-apps/api/core");
    return isTauri();
  } catch {
    return false; // dev browser: the module is not resolvable
  }
}

export async function openPerformerFolder(performerId: string): Promise<void> {
  if (await inTauri()) {
    const { invoke } = await import("@tauri-apps/api/core");
    await invoke("open_performer", { performerId });
    return;
  }
  // Dev browser: no shell to launch through, so the server launches — the
  // pre-#31 behaviour, foreground caveat and all.
  const r = await openPerformerApiPerformersPerformerIdOpenPost({
    path: { performer_id: performerId },
  });
  if (r.error) throw r.error;
}

export async function openArchivePair(subdir: string, performerId: string): Promise<void> {
  if (await inTauri()) {
    const { invoke } = await import("@tauri-apps/api/core");
    await invoke("open_pair", { subdir, performerId });
    return;
  }
  const r = await openPairApiOpenPairPost({ body: { subdir, performer_id: performerId } });
  if (r.error) throw r.error;
}

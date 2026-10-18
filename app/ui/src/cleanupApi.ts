import { archiveRequest } from "./archiveApi";

export type CleanupContext = { id: string; batch_path: string; batch_revision: string };
export type CleanupTask = {
  id: string;
  context_id: string;
  request_id: string;
  batch_path: string;
  batch_revision: string;
  status: "queued" | "running" | "completed" | "failed";
  current_name: string | null;
  checked_count: number;
  total_count: number;
  deleted_count: number;
  kept_count: number;
  skipped_count: number;
  failed_count: number;
  message: string | null;
  items: {
    name: string;
    status: "deleted" | "not_empty" | "skipped" | "failed";
    message: string | null;
  }[];
};

export function readCleanupContext(
  batch_path: string,
  signal?: AbortSignal,
): Promise<CleanupContext> {
  return archiveRequest("/cleanup-contexts", {
    method: "POST",
    body: JSON.stringify({ batch_path }),
    signal,
  });
}

export function createCleanup(body: {
  context_id: string;
  request_id: string;
}): Promise<CleanupTask> {
  return archiveRequest("/cleanups", { method: "POST", body: JSON.stringify(body) });
}

export function getLatestCleanup(signal?: AbortSignal): Promise<CleanupTask | null> {
  return archiveRequest("/cleanups/latest", { signal });
}

export function getCleanup(id: string, signal?: AbortSignal): Promise<CleanupTask> {
  return archiveRequest(`/cleanups/${encodeURIComponent(id)}`, { signal });
}

export function isCleanupActive(task: CleanupTask | null | undefined): boolean {
  return task?.status === "queued" || task?.status === "running";
}

import { serverBase, sessionHeaders } from "./api";

export type ArchiveFolder = {
  name: string;
  size_bytes: number | null;
  files: number | null;
  error: string | null;
};

export type ArchiveContext = {
  id: string;
  local_dir: string;
  subdir: string;
  performer_id: string;
  performer_name: string;
  source_label: string | null;
  has_thumb: boolean;
  source_path: string;
  target_path: string;
  source_items: ArchiveFolder[];
  target_items: ArchiveFolder[];
  source_error: string | null;
  target_error: string | null;
  source_retryable?: boolean;
  target_retryable?: boolean;
};

export type ArchiveTask = {
  id: string;
  request_id: string;
  context_id: string;
  local_dir: string;
  subdir: string;
  performer_id: string;
  performer_name: string;
  source_label: string | null;
  has_thumb: boolean;
  source_path: string;
  target_path: string;
  status: "queued" | "running" | "completed" | "stopped" | "failed" | "needs_review";
  phase: "preflight" | "copying" | "verifying" | "committing" | "cleaning" | "finished";
  current_name: string | null;
  progress_bytes: number;
  total_bytes: number;
  verified_bytes: number;
  completed_count: number;
  total_count: number;
  conflicts: string[];
  message: string | null;
  stop_requested: boolean;
  created_at: string;
  updated_at: string;
  items: {
    name: string;
    status: string;
    size_bytes: number | null;
    progress_bytes: number;
    message: string | null;
  }[];
};

export class ArchiveApiError extends Error {
  constructor(
    readonly detail: string,
    readonly status: number,
  ) {
    super(detail);
    this.name = "ArchiveApiError";
  }
}

export async function archiveRequest<T>(path: string, options: RequestInit = {}): Promise<T> {
  // A request timeout only stops waiting for the response. It never implies
  // that a submitted transfer stopped; the persistent task is the authority.
  const signal = options.signal
    ? AbortSignal.any([options.signal, AbortSignal.timeout(30_000)])
    : AbortSignal.timeout(30_000);
  const response = await fetch(`${serverBase()}/api/archive${path}`, {
    ...options,
    signal,
    headers: { "Content-Type": "application/json", ...sessionHeaders() },
  });
  const data = await response.json();
  if (!response.ok) {
    const detail = typeof data?.detail === "string" ? data.detail : "归档请求失败";
    throw new ArchiveApiError(detail, response.status);
  }
  return data as T;
}

export function createArchiveContext(
  body: { local_dir: string; subdir: string; performer_id: string },
  signal?: AbortSignal,
): Promise<ArchiveContext> {
  return archiveRequest("/contexts", { method: "POST", body: JSON.stringify(body), signal });
}

export function createTransfer(body: {
  context_id: string;
  names: string[];
  request_id: string;
}): Promise<ArchiveTask> {
  return archiveRequest("/transfers", { method: "POST", body: JSON.stringify(body) });
}

export function getLatestTransfer(signal?: AbortSignal): Promise<ArchiveTask | null> {
  return archiveRequest("/transfers/latest", { signal });
}

export function getTransfer(id: string, signal?: AbortSignal): Promise<ArchiveTask> {
  return archiveRequest(`/transfers/${encodeURIComponent(id)}`, { signal });
}

export function stopTransfer(id: string): Promise<ArchiveTask> {
  return archiveRequest(`/transfers/${encodeURIComponent(id)}/stop`, { method: "POST" });
}

export function isTransferActive(task: ArchiveTask | null | undefined): boolean {
  return task?.status === "queued" || task?.status === "running";
}

export function archiveError(error: unknown, fallback: string): string {
  if (error && typeof error === "object" && "detail" in error && typeof error.detail === "string") {
    return error.detail;
  }
  return error instanceof Error ? error.message : fallback;
}

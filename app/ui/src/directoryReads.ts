import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef } from "react";
import { type ArchiveContext, archiveError, createArchiveContext } from "./archiveApi";

export const DIRECTORY_RETRIES = 3;

export class DirectoryReadError extends Error {
  readonly detail: string;
  constructor(
    message: string,
    readonly retryable: boolean,
    readonly snapshot?: ArchiveContext,
  ) {
    super(message);
    this.name = "DirectoryReadError";
    this.detail = message;
  }
}

export function transientDirectoryError(error: unknown): boolean {
  if (error instanceof DirectoryReadError) return error.retryable;
  if (!error || typeof error !== "object") return false;
  const status = "status" in error ? error.status : undefined;
  if (typeof status === "number") return [408, 502, 503, 504].includes(status);
  // Permission, missing paths and invalid bindings must not be retried by
  // guessing from localized text. Fetch network failures and timeouts are explicit.
  return error instanceof TypeError || ("name" in error && error.name === "TimeoutError");
}

export function retryDirectoryRead(failureCount: number, error: unknown): boolean {
  return failureCount < DIRECTORY_RETRIES && transientDirectoryError(error);
}

export function directoryRetryDelay(attempt: number): number {
  return 1000 * 2 ** Math.min(attempt, 2);
}

export function useArchiveContext(localDir: string, subdir: string, performerId: string) {
  const client = useQueryClient();
  const key = `${localDir}\0${subdir}\0${performerId}`;
  const snapshot = useRef<{ key: string; data?: ArchiveContext }>({ key });
  if (snapshot.current.key !== key) snapshot.current = { key };
  const query = useQuery({
    queryKey: ["archive-context", localDir, subdir, performerId],
    queryFn: async ({ signal }) => {
      try {
        const next = await createArchiveContext(
          { local_dir: localDir, subdir, performer_id: performerId },
          signal,
        );
        signal.throwIfAborted();
        const previous =
          snapshot.current.key === key
            ? (snapshot.current.data ??
              client.getQueryData<ArchiveContext>([
                "archive-context",
                localDir,
                subdir,
                performerId,
              ]))
            : undefined;
        const observed = {
          ...next,
          source_items: next.source_error ? (previous?.source_items ?? []) : next.source_items,
          target_items: next.target_error ? (previous?.target_items ?? []) : next.target_items,
        };
        if (snapshot.current.key === key) snapshot.current.data = observed;
        if (next.source_error || next.target_error) {
          throw new DirectoryReadError(
            [next.source_error, next.target_error].filter(Boolean).join("；"),
            (!next.source_error || !!next.source_retryable) &&
              (!next.target_error || !!next.target_retryable),
            observed,
          );
        }
        return observed;
      } catch (error) {
        signal.throwIfAborted();
        if (error instanceof DirectoryReadError) throw error;
        throw new DirectoryReadError(
          archiveError(error, "目录读取失败"),
          transientDirectoryError(error),
          snapshot.current.key === key ? snapshot.current.data : undefined,
        );
      }
    },
    retry: retryDirectoryRead,
    retryDelay: directoryRetryDelay,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    staleTime: 0,
  });
  const failure = query.failureReason ?? query.error;
  return {
    ...query,
    data: (failure instanceof DirectoryReadError ? failure.snapshot : undefined) ?? query.data,
    retrying: query.isFetching && query.failureCount > 0,
  };
}

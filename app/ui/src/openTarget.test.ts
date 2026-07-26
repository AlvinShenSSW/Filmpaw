/** #31: the shell/browser split lives in ONE place, so it gets one test.
 *
 * What matters in both branches: only INTENT parameters travel (an id, a subdir
 * name — never a path), and the server's `{status, detail}` reaches the caller
 * unchanged so the pages' `detailOf(e)` keeps working. */

import { beforeEach, describe, expect, it, vi } from "vitest";

let tauri = false;
const invoke = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({ isTauri: () => tauri, invoke }));

vi.mock("./client", () => ({
  openPairApiOpenPairPost: vi.fn(),
  openPerformerApiPerformersPerformerIdOpenPost: vi.fn(),
}));

import { openPairApiOpenPairPost, openPerformerApiPerformersPerformerIdOpenPost } from "./client";
import { openArchivePair, openPerformerFolder } from "./openTarget";

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(openPairApiOpenPairPost).mockResolvedValue({ data: null } as never);
  vi.mocked(openPerformerApiPerformersPerformerIdOpenPost).mockResolvedValue({
    data: null,
  } as never);
  invoke.mockResolvedValue(undefined);
});

describe("packaged app (Tauri): the SHELL opens, so the window comes to the front", () => {
  beforeEach(() => {
    tauri = true;
  });

  it("single open goes through the shell command and never hits /open", async () => {
    await openPerformerFolder("pid-1");
    expect(invoke).toHaveBeenCalledWith("open_performer", { performerId: "pid-1" });
    expect(openPerformerApiPerformersPerformerIdOpenPost).not.toHaveBeenCalled();
  });

  it("pair passes the subdir NAME and the id — never a path", async () => {
    await openArchivePair("胡桃さくら,新井リマ", "pid-2");
    expect(invoke).toHaveBeenCalledWith("open_pair", {
      subdir: "胡桃さくら,新井リマ",
      performerId: "pid-2",
    });
    const [, args] = invoke.mock.calls[0];
    expect(JSON.stringify(args)).not.toContain("local_dir");
    expect(openPairApiOpenPairPost).not.toHaveBeenCalled();
  });

  it("rejects with the server's {status, detail} so detailOf() still works", async () => {
    invoke.mockRejectedValue({ status: 409, detail: "该记录已失效, 无法双开" });
    await expect(openArchivePair("小红", "pid-3")).rejects.toMatchObject({
      detail: "该记录已失效, 无法双开",
    });
  });
});

describe("dev browser: no shell, so the server opens (pre-#31 behaviour)", () => {
  beforeEach(() => {
    tauri = false;
  });

  it("single open falls back to POST /open", async () => {
    await openPerformerFolder("pid-1");
    expect(openPerformerApiPerformersPerformerIdOpenPost).toHaveBeenCalledWith({
      path: { performer_id: "pid-1" },
    });
    expect(invoke).not.toHaveBeenCalled();
  });

  it("pair falls back to POST /open-pair with the same intent parameters", async () => {
    await openArchivePair("小红", "pid-2");
    expect(openPairApiOpenPairPost).toHaveBeenCalledWith({
      body: { subdir: "小红", performer_id: "pid-2" },
    });
    expect(invoke).not.toHaveBeenCalled();
  });

  it("surfaces the server error object unchanged", async () => {
    vi.mocked(openPairApiOpenPairPost).mockResolvedValue({
      error: { detail: "本地目录不存在 — 请重新选择" },
    } as never);
    await expect(openArchivePair("小红", "pid-3")).rejects.toMatchObject({
      detail: "本地目录不存在 — 请重新选择",
    });
  });
});

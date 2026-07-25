/** Focused unit tests for the shared directory picker (Kimi #19 minor). */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { pickDirectory } from "./pickDirectory";

vi.mock("@tauri-apps/api/core", () => ({ isTauri: () => false }));

describe("pickDirectory (dev fallback)", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("returns the trimmed path for valid input", async () => {
    vi.spyOn(window, "prompt").mockReturnValue("  D:Downloads  ");
    expect(await pickDirectory("t")).toBe("D:Downloads");
  });

  it("returns null on cancel", async () => {
    vi.spyOn(window, "prompt").mockReturnValue(null);
    expect(await pickDirectory("t")).toBeNull();
  });

  it("returns null on empty/whitespace input", async () => {
    vi.spyOn(window, "prompt").mockReturnValue("   ");
    expect(await pickDirectory("t")).toBeNull();
  });
});

/** Focused unit tests for the shared directory picker (Kimi #19 minor). */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { pickDirectory } from "./pickDirectory";

let tauri = false;
vi.mock("@tauri-apps/api/core", () => ({ isTauri: () => tauri }));
const openMock = vi.fn();
vi.mock("@tauri-apps/plugin-dialog", () => ({ open: () => openMock() }));

describe("pickDirectory (dev fallback)", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    tauri = false;
  });

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

describe("pickDirectory (tauri branch)", () => {
  it("coerces an empty tauri result to null (contract parity)", async () => {
    tauri = true;
    openMock.mockResolvedValue("");
    expect(await pickDirectory("t")).toBeNull();
    openMock.mockResolvedValue("D:Movies");
    expect(await pickDirectory("t")).toBe("D:Movies");
    tauri = false;
  });
});

/** WCAG AA contrast regression for the design tokens (Kimi #20 P1/P2). */
import { describe, expect, it } from "vitest";
import { theme, tokens } from "./theme";

function luminance(hex: string): number {
  const n = Number.parseInt(hex.slice(1), 16);
  const ch = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((v) => {
    const c = v / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2];
}
function ratio(a: string, b: string): number {
  const [l1, l2] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (l1 + 0.05) / (l2 + 0.05);
}

describe("token contrast (WCAG AA >= 4.5:1 for text)", () => {
  it("filled-button ink text on the orange brand passes", () => {
    expect(ratio(tokens.ink, tokens.orange)).toBeGreaterThanOrEqual(4.5);
    expect(theme.palette.primary.contrastText).toBe(tokens.ink);
  });

  it("filled-button hover stays light enough for ink text", () => {
    expect(ratio(tokens.ink, tokens.orangeHover)).toBeGreaterThanOrEqual(4.5);
  });

  it("secondary/caption muted text on white passes", () => {
    expect(ratio(tokens.muted, "#FFFFFF")).toBeGreaterThanOrEqual(4.5);
    expect(ratio(tokens.muted, tokens.paper)).toBeGreaterThanOrEqual(4.5);
  });

  it("status text passes on its own tint background (the real pairing)", () => {
    expect(ratio(tokens.ok, tokens.okBg)).toBeGreaterThanOrEqual(4.5);
    expect(ratio(tokens.bad, tokens.badBg)).toBeGreaterThanOrEqual(4.5);
    expect(ratio(tokens.ok, "#FFFFFF")).toBeGreaterThanOrEqual(4.5);
    expect(ratio(tokens.bad, "#FFFFFF")).toBeGreaterThanOrEqual(4.5);
  });
});

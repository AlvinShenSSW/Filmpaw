import { createTheme } from "@mui/material/styles";

/**
 * Filmpaw design system (issue #20).
 *
 * Method adopted from `ui-ux-pro-max --design-system` (productivity-tool
 * profile): a single typographic scale, an 8px spacing grid, consistent
 * button sizing with no text overflow, 150–300ms hover transitions, and the
 * accessibility checklist (focus-visible, 4.5:1 contrast, reduced-motion).
 * The PALETTE stays the operator-specified brand (orange/white/warm-gray,
 * decision D) — the skill's teal is not adopted.
 *
 * All colors, sizes, and radii live here; components consume the tokens
 * instead of hard-coding hex/px (which was the source of the drift).
 */

export const tokens = {
  orange: "#EF9F27",
  orangeHover: "#E2921D", // filled-button hover; keeps ink text at AA
  orangeDeep: "#B45E14",
  orangeText: "#7A4A0C", // selected-item label on orangeSoft
  orangeSoft: "#FDF3E3",
  hoverBg: "#F7F5F1", // neutral row/button hover
  ink: "#33322F",
  muted: "#6B675E", // WCAG AA on white (5.67:1); was #8A867E (3.4:1, failed)
  line: "#ECEAE4",
  lineStrong: "#DDD9D1",
  paper: "#FAF9F6",
  ok: "#3B6D11",
  okBg: "#EAF3DE",
  okBorder: "#C4DD9D",
  bad: "#A32D2D",
  badBg: "#FCEBEB",
  badBorder: "#F0B8B8",
  mono: '"Cascadia Mono", Consolas, monospace',
} as const;

export const RAIL_WIDTH = 56;

export const theme = createTheme({
  palette: {
    mode: "light",
    // Filled buttons use INK text on the orange brand (5.77:1 AA) — white on
    // #EF9F27 was only 2.17:1. The brand orange is kept; contrast comes from
    // the dark label, and hover stays light (see MuiButton override) so the
    // ratio holds. This satisfies the a11y checklist this theme documents.
    primary: { main: tokens.orange, dark: tokens.orangeDeep, contrastText: tokens.ink },
    success: { main: tokens.ok },
    error: { main: tokens.bad },
    background: { default: "#FFFFFF", paper: tokens.paper },
    text: { primary: tokens.ink, secondary: tokens.muted, disabled: "#B3AFA6" },
    divider: tokens.line,
  },
  shape: { borderRadius: 9 },
  spacing: 8, // explicit 8px grid
  typography: {
    fontFamily: '"Microsoft YaHei UI","PingFang SC","Noto Sans SC",system-ui,sans-serif',
    // One scale — every surface picks from these, no inline fontSize.
    h6: { fontSize: 18, fontWeight: 600, lineHeight: 1.4 },
    subtitle2: { fontSize: 14, fontWeight: 600, lineHeight: 1.5 },
    body2: { fontSize: 13, lineHeight: 1.6 },
    caption: { fontSize: 12, lineHeight: 1.5, color: tokens.muted },
    button: { fontSize: 13, fontWeight: 600, textTransform: "none" },
  },
  components: {
    MuiCssBaseline: {
      styleOverrides: {
        // Reduce only transitions (decorative). !important is required —
        // component `transition` shorthands (e.g. MuiButton) otherwise win.
        // Keyframe animations (loading spinner) use animationDuration and
        // are intentionally left running.
        "@media (prefers-reduced-motion: reduce)": {
          "*": { transitionDuration: "0.01ms !important" },
        },
      },
    },
    MuiButton: {
      defaultProps: { disableElevation: true },
      styleOverrides: {
        root: {
          minWidth: 44, // keep a touch-target floor; nowrap+padding prevent CJK clipping
          whiteSpace: "nowrap",
          paddingLeft: 14,
          paddingRight: 14,
          borderRadius: 9,
          transition: "background-color 180ms ease, border-color 180ms ease",
          "&:focus-visible": { outline: `2px solid ${tokens.orangeDeep}`, outlineOffset: 2 },
        },
        // Hover must stay light so INK text keeps AA contrast (the MUI
        // default darkens to primary.dark, which would drop the ratio).
        containedPrimary: { "&:hover": { backgroundColor: tokens.orangeHover } },
        outlinedInherit: { borderColor: tokens.lineStrong, color: tokens.muted },
        sizeSmall: { paddingTop: 5, paddingBottom: 5 },
      },
    },
    MuiIconButton: {
      styleOverrides: {
        root: {
          borderRadius: 8,
          "&:focus-visible": { outline: `2px solid ${tokens.orangeDeep}`, outlineOffset: 2 },
        },
      },
    },
    MuiTextField: { defaultProps: { size: "small" } },
    MuiChip: {
      styleOverrides: { root: { borderRadius: 999, height: 24, fontSize: 12 } },
    },
    MuiTooltip: {
      styleOverrides: { tooltip: { fontSize: 12 } },
    },
  },
});

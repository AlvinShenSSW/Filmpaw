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
  orangeDeep: "#B45E14",
  orangeSoft: "#FDF3E3",
  ink: "#33322F",
  muted: "#8A867E",
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
    primary: { main: tokens.orange, dark: tokens.orangeDeep, contrastText: "#FFFFFF" },
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
        "@media (prefers-reduced-motion: reduce)": {
          "*": { transitionDuration: "0.01ms !important", animationDuration: "0.01ms !important" },
        },
      },
    },
    MuiButton: {
      defaultProps: { disableElevation: true },
      styleOverrides: {
        root: {
          minWidth: 0, // let padding size the button; never clip CJK labels
          whiteSpace: "nowrap",
          paddingLeft: 14,
          paddingRight: 14,
          borderRadius: 9,
          transition: "background-color 180ms ease, border-color 180ms ease",
          "&:focus-visible": { outline: `2px solid ${tokens.orangeDeep}`, outlineOffset: 2 },
        },
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

import { createTheme } from "@mui/material/styles";

/** Palette per design spec: orange #EF9F27 / deep #B45E14 / white / warm grays. */
export const theme = createTheme({
  palette: {
    mode: "light",
    primary: { main: "#EF9F27", dark: "#B45E14", contrastText: "#FFFFFF" },
    background: { default: "#FFFFFF", paper: "#FAF9F6" },
    text: { primary: "#33322F", secondary: "#8A867E" },
  },
  shape: { borderRadius: 9 },
  typography: {
    fontFamily: '"Microsoft YaHei UI","PingFang SC","Noto Sans SC",system-ui,sans-serif',
  },
});

export const RAIL_WIDTH = 56;

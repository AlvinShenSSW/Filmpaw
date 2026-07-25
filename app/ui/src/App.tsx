import GroupsIcon from "@mui/icons-material/Groups";
import SettingsIcon from "@mui/icons-material/Settings";
import SwapHorizIcon from "@mui/icons-material/SwapHoriz";
import Box from "@mui/material/Box";
import IconButton from "@mui/material/IconButton";
import Tooltip from "@mui/material/Tooltip";
import {
  createRootRoute,
  createRoute,
  createRouter,
  Link,
  Outlet,
  RouterProvider,
} from "@tanstack/react-router";
import { ArchivePage, PerformersPage, SettingsPage } from "./pages";
import { RAIL_WIDTH, tokens } from "./theme";

function RailButton({
  to,
  label,
  children,
}: {
  to: string;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <Tooltip title={label} placement="right">
      <Link to={to} aria-label={label} style={{ textDecoration: "none" }}>
        {({ isActive }: { isActive: boolean }) => (
          <IconButton
            sx={{
              width: 40,
              height: 40,
              borderRadius: "10px",
              color: isActive ? tokens.ink : "text.secondary",
              bgcolor: isActive ? "primary.main" : "transparent",
              "&:hover": { bgcolor: isActive ? "primary.main" : tokens.hoverBg },
            }}
          >
            {children}
          </IconButton>
        )}
      </Link>
    </Tooltip>
  );
}

function Shell() {
  return (
    <Box sx={{ display: "flex", height: "100vh" }}>
      <Box
        component="nav"
        aria-label="主导航"
        sx={{
          width: RAIL_WIDTH,
          flexShrink: 0,
          borderRight: "1px solid #ECEAE4",
          bgcolor: "background.paper",
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          py: 1.5,
          gap: 1,
        }}
      >
        <RailButton to="/" label="表演者库">
          <GroupsIcon fontSize="small" />
        </RailButton>
        <RailButton to="/archive" label="归档对比">
          <SwapHorizIcon fontSize="small" />
        </RailButton>
        <Box sx={{ flex: 1 }} />
        <RailButton to="/settings" label="设置">
          <SettingsIcon fontSize="small" />
        </RailButton>
      </Box>
      <Box component="main" sx={{ flex: 1, minWidth: 0, overflow: "auto" }}>
        <Outlet />
      </Box>
    </Box>
  );
}

const rootRoute = createRootRoute({ component: Shell });
const performersRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  component: PerformersPage,
});
const archiveRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/archive",
  component: ArchivePage,
});
const settingsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/settings",
  component: SettingsPage,
});

const router = createRouter({
  routeTree: rootRoute.addChildren([performersRoute, archiveRoute, settingsRoute]),
});

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}

export function App() {
  return <RouterProvider router={router} />;
}

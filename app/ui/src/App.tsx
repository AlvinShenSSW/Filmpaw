import GroupsIcon from "@mui/icons-material/Groups";
import InfoOutlinedIcon from "@mui/icons-material/InfoOutlined";
import SettingsIcon from "@mui/icons-material/Settings";
import SwapHorizIcon from "@mui/icons-material/SwapHoriz";
import Box from "@mui/material/Box";
import Tooltip from "@mui/material/Tooltip";
import {
  createRootRoute,
  createRoute,
  createRouter,
  Link,
  Outlet,
  RouterProvider,
} from "@tanstack/react-router";
import { AboutPage, ArchivePage, PerformersPage, SettingsPage } from "./pages";
import { RAIL_WIDTH, tokens } from "./theme";

/** One nav item = ONE interactive element.
 *
 * This used to be `<Link><IconButton/></Link>` — an anchor wrapping a button.
 * That is two nested interactive controls: two tab stops for one destination,
 * and the inner button has no accessible name of its own (#34). The link is now
 * the only control; the icon is decoration inside it.
 *
 * Active styling keys off `data-status="active"`, which TanStack Router sets on
 * the active link (it also sets `aria-current="page"` — so the current page is
 * announced, not just coloured).
 *
 * 40×40 is deliberate: the 44×44 minimum in the design guidance is scoped to
 * touch UI, and this is a mouse-driven desktop shell whose rail is 56px wide.
 */
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
      <Box
        component={Link}
        to={to}
        aria-label={label}
        sx={{
          width: 40,
          height: 40,
          borderRadius: "10px",
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          textDecoration: "none",
          color: "text.secondary",
          "&:hover": { bgcolor: tokens.hoverBg },
          "&[data-status='active']": {
            color: tokens.ink,
            bgcolor: "primary.main",
            "&:hover": { bgcolor: "primary.main" },
          },
          // The MUI ripple/focus ring went away with IconButton — put a visible
          // one back, or keyboard users lose track of where they are.
          "&:focus-visible": {
            outline: `2px solid ${tokens.orangeDeep}`,
            outlineOffset: "2px",
          },
        }}
      >
        {children}
      </Box>
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
          borderRight: `1px solid ${tokens.line}`,
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
        <RailButton to="/about" label="关于">
          <InfoOutlinedIcon fontSize="small" />
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

const aboutRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/about",
  component: AboutPage,
});

/** Exported so tests can mount the REAL tree on a memory history — asserting a
 * page's content through routing catches a missing route or a broken rail link,
 * which rendering the component directly never would (#34). */
export const routeTree = rootRoute.addChildren([
  performersRoute,
  archiveRoute,
  settingsRoute,
  aboutRoute,
]);

const router = createRouter({ routeTree });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}

export function App() {
  return <RouterProvider router={router} />;
}

# filmpaw-ui

React 19 + MUI 7 + TanStack Router/Query, built with rsbuild.

```bash
pnpm install
pnpm dev          # http://localhost:3000 (expects server on 8720 outside Tauri)
pnpm typecheck
pnpm run ci       # biome
pnpm build
```

The Tauri shell creates a fresh session credential on every launch and supplies
it to the UI and sidecar automatically. For standalone browser development,
start both processes with the same `FILMPAW_SESSION_TOKEN` environment variable
(64 hexadecimal characters, generated with `secrets.token_hex(32)`)
and set `FILMPAW_DEV=1` for the server. Rsbuild injects this credential only for
`pnpm dev`; built bundles always omit it. Missing or incorrect credentials leave
server mutations and archive operations unavailable.

Every development or smoke-test server must use `FILMPAW_DB` pointing to a
temporary file. Never run development fixtures against the default user library.

Closing the desktop app during a transfer offers continuing, waiting for the
queue to finish, or stopping the queue and waiting for a safe exit. A server-side
admission check prevents a new transfer from starting while the shell exits.

The archive sidebar can clean strictly empty first-level actor directories in the
selected batch, including a NAS temporary batch. Cleanup has a separate session
status provider and no automatic mutation retries. Transfer and cleanup buttons
share busy/unknown-state guards, backed by server admission. During cleanup the
exit dialog offers continuing or waiting; a cleanup ID is never sent to the
transfer-stop endpoint. Confirmed deletions and a failed list refresh are shown
separately so a refresh error cannot erase an already-known cleanup result.

Browser development requires the same `FILMPAW_SESSION_TOKEN` as the server,
with `FILMPAW_DEV=1`. See [server setup](../server/README.md). Rsbuild injects this
credential only into the development bundle; the packaged Tauri shell supplies
its own fresh token at runtime. A token is never an URL/query parameter.

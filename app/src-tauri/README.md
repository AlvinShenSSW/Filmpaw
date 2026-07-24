# filmpaw (Tauri shell)

Tauri 2 shell. Spawns `filmpaw-server` (dev: via `uv run --project ../server`),
reads `FILMPAW_PORT=<n>` from its stdout, injects `window.__FILMPAW_PORT__`,
and kills the child on window close.

```bash
# from app/ (tauri CLI must find src-tauri as a subfolder of cwd):
ui\node_modules\.bin\tauri.cmd dev     # dev window (starts rsbuild + spawns server)
ui\node_modules\.bin\tauri.cmd build   # NSIS bundle (wired fully in issue #7)
```

Note: running `pnpm tauri dev` from `app/ui` does NOT work — the CLI looks for
`src-tauri` under the current directory, so invoke from `app/`.

`cargo check` from this directory verifies the Rust side alone.

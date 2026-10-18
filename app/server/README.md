# filmpaw-server

FastAPI sidecar for the Filmpaw desktop app.

```bash
uv sync                    # install deps (Python 3.13 managed by uv)
uv run pytest              # tests
uv run filmpaw-server      # serve on an OS-assigned port (prints FILMPAW_PORT=<n>)
uv run filmpaw-server --port 8720   # fixed port (dev convention)
```

Tauri supplies a fresh session token automatically. For browser-only development,
start both processes from shells with the **same** `FILMPAW_SESSION_TOKEN` (64 hex
characters) and `FILMPAW_DEV=1`. Generate a temporary token in PowerShell without
printing it; the UI dev process inherits it through Rsbuild:

```powershell
$env:FILMPAW_SESSION_TOKEN = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
$env:FILMPAW_DEV = '1'
$env:FILMPAW_DB = Join-Path $env:TEMP ('filmpaw-dev-' + [Guid]::NewGuid() + '.db')
# Start the server and `pnpm dev` from this environment.
```

All mutation requests and archive reads require `X-Filmpaw-Session`. Missing or
invalid configuration disables writes; CORS and development mode never bypass
authorization. Production UI builds never embed the development token. Do not
commit tokens or put them in URLs.

Archive contexts bind the approved batch and a performer ID. Tasks persist in
schema v3; recovery only reconciles existing evidence and never resumes moving
or deleting automatically. All tests use disposable directories and databases.

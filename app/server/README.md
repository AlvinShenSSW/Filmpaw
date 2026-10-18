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

Named NTFS data streams (ADS) are currently unsupported: a selected movie that
contains them fails preflight without moving any selected movie. Source metadata
is checked again before cleanup and on the exact handle before deletion; a late
stream produces a retained source / cleanup-pending result. Unverifiable stream
metadata fails closed. Stop external writes to selected movies during transfer:
Windows sharing is per stream, so the final check and deletion are not atomic
against another process adding a stream.

Empty-actor cleanup uses a separate session-local worker under the same admission
lock as transfers and exit. Its capability binds the explicitly selected batch,
settings revision and native directory identity; the delete request contains no
paths or actor names. Local and NAS temporary batches use the same boundary.
Hidden contents, subdirectories, named streams, links and unverifiable metadata
are never treated as empty. Windows deletes through the verified directory handle
without recursive traversal. Cleanup results survive UI reloads in that sidecar
session; restarting invalidates capabilities and never resumes deletion. No new
database schema or persistent cleanup history is introduced.

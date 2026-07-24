# filmpaw-server

FastAPI sidecar for the Filmpaw desktop app.

```bash
uv sync                    # install deps (Python 3.13 managed by uv)
uv run pytest              # tests
uv run filmpaw-server      # serve on an OS-assigned port (prints FILMPAW_PORT=<n>)
uv run filmpaw-server --port 8720   # fixed port (dev convention)
```

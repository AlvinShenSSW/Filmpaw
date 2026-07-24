"""Entry point: bind 127.0.0.1, announce the port on stdout, serve.

The Tauri shell launches this as a sidecar, reads the FILMPAW_PORT line
from stdout, and injects the port into the UI. Port 0 (default) lets the
OS pick a free port.
"""

import argparse
import socket
import sys

import uvicorn

from filmpaw_server.app import create_app

HOST = "127.0.0.1"


def main() -> None:
    parser = argparse.ArgumentParser(prog="filmpaw-server")
    parser.add_argument("--port", type=int, default=0, help="port to bind (0 = OS-assigned)")
    args = parser.parse_args()

    # Bind the socket ourselves so an OS-assigned port is known before serving,
    # then hand the open socket to uvicorn (no close/rebind race).
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HOST, args.port))
    port = sock.getsockname()[1]

    print(f"FILMPAW_PORT={port}", flush=True)
    sys.stdout.flush()

    config = uvicorn.Config(create_app(), host=HOST, port=port, log_level="info")
    server = uvicorn.Server(config)
    server.run(sockets=[sock])


if __name__ == "__main__":
    main()

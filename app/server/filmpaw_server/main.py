"""Entry point: bind 127.0.0.1, announce the port on stdout, serve.

The Tauri shell reads FILMPAW_PORT and FILMPAW_PID from stdout, injects the
port into the UI, and tracks the actual Python worker's lifetime. Port 0
(default) lets the OS pick a free port.
"""

import argparse
import os
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
    if sys.platform != "win32":
        # On Windows SO_REUSEADDR allows port sharing/hijack and defeats
        # "address already in use" detection, so only set it elsewhere.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HOST, args.port))
    port = sock.getsockname()[1]

    print(f"FILMPAW_PORT={port}", flush=True)
    # Preserve the first-line port contract used by packaging checks. The
    # next line identifies THIS worker, not uv or the PyInstaller bootloader.
    print(f"FILMPAW_PID={os.getpid()}", flush=True)
    sys.stdout.flush()

    config = uvicorn.Config(create_app(), host=HOST, port=port, log_level="info")
    server = uvicorn.Server(config)
    server.run(sockets=[sock])


if __name__ == "__main__":
    main()

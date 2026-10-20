"""The shell must track the Python worker, not uv or a onefile bootloader."""

import os
from types import SimpleNamespace

from filmpaw_server import main as entry


def test_startup_keeps_port_first_and_announces_real_worker_pid(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["filmpaw-server"])
    seen = {}
    monkeypatch.setattr(entry, "create_app", lambda port=None: seen.setdefault("port", port))
    monkeypatch.setattr(entry.uvicorn, "Config", lambda *args, **kwargs: None)
    def run(*, sockets):
        for sock in sockets:
            sock.close()

    monkeypatch.setattr(entry.uvicorn, "Server", lambda config: SimpleNamespace(run=run))
    entry.main()
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("FILMPAW_PORT=")
    assert 0 < int(lines[0].split("=", 1)[1]) < 65536
    assert lines[1] == f"FILMPAW_PID={os.getpid()}"
    assert seen["port"] == int(lines[0].split("=", 1)[1])  # endpoint file announces the bound port

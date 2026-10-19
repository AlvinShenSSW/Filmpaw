"""The shell must track the Python worker, not uv or a onefile bootloader."""

import os
from types import SimpleNamespace

from filmpaw_server import main as entry


def test_startup_keeps_port_first_and_announces_real_worker_pid(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["filmpaw-server"])
    monkeypatch.setattr(entry, "create_app", lambda: None)
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


def test_fixed_worker_branch_never_constructs_app_or_socket(monkeypatch):
    from filmpaw_server import scan_worker
    monkeypatch.setattr("sys.argv", ["filmpaw-server", "--workflow-nas-worker"])
    def forbidden(*args, **kwargs):
        raise AssertionError("worker must not construct service resources")
    monkeypatch.setattr(entry, "create_app", forbidden)
    monkeypatch.setattr(entry.socket, "socket", forbidden)
    monkeypatch.setattr(scan_worker, "worker_main", lambda: 7)
    assert entry.main() == 7

"""Reproducible disposable Windows SMB/long-path checks for issue #36.

Run from the repo root:
    uv run --project app/server --locked python docs/evidence/issue-36/smb-smoke.py

Uses only a newly created TEMP tree, existing localhost C$ (no share/permission
changes), and an explicitly isolated FILMPAW_DB. Writes the adjacent JSON report.
"""

import json
import os
import platform
import tempfile
import threading
import time
import winreg
from datetime import datetime, timezone
from pathlib import Path

from filmpaw_server.db import connect
from filmpaw_server.transfer import TransferService


def service_for(root, source, target):
    database = root / "fixture.db"
    os.environ["FILMPAW_DB"] = str(database)
    conn = connect(database)
    conn.execute("INSERT INTO settings(key,value) VALUES ('last_local_dir',?)", (str(source.parent),))
    conn.execute("INSERT INTO sources(id,unc_path,label) VALUES (1,?,'Disposable fixture')", (str(target.parent),))
    conn.execute(
        "INSERT INTO performers(id,name,name_norm,source_id,folder_name,unc_path,first_seen_at,last_seen_at)"
        " VALUES ('actor','Actor','actor',1,'Actor',?,'now','now')", (str(target),)
    )
    conn.commit()
    return TransferService(conn, threading.RLock(), auto_start=False), conn


def execute(service, source, name):
    context = service.create_context(str(source.parent), "Actor", "actor")
    assert not context["source_error"] and not context["target_error"], context
    task = service.submit(context["id"], [name], "disposable-smoke")
    service._run(task["id"])
    return service.get_task(task["id"])


def smb_case():
    with tempfile.TemporaryDirectory(prefix="filmpaw-smb-evidence-") as temporary:
        root = Path(temporary).resolve()
        assert root.parent == Path(tempfile.gettempdir()).resolve()
        assert root.name.startswith("filmpaw-smb-evidence-")
        source, local_target = root / "batch" / "Actor", root / "nas" / "Actor"
        source.mkdir(parents=True)
        local_target.mkdir(parents=True)
        assert local_target.drive.upper() == "C:"
        target = Path(r"\\localhost\C$").joinpath(*local_target.parts[1:])
        name = "作品 001, 特别版"
        movie = source / name
        (movie / "subs").mkdir(parents=True)
        (movie / "empty").mkdir()
        with (movie / "video.mp4").open("wb") as output:
            for _ in range(32):
                output.write(bytes(range(256)) * 4096)
        (movie / "movie.nfo").write_bytes(b"<movie>synthetic fixture</movie>")
        (movie / "subs" / "zh.srt").write_bytes(b"synthetic subtitle")
        os.utime(movie / "video.mp4", ns=(1700000000000000000, 1700000000000000000))
        (source / "folder.jpg").write_bytes(b"actor portrait must stay")
        service, conn = service_for(root, source, target)
        start = time.monotonic()
        try:
            task = execute(service, source, name)
            copied = target / name
            result = {
                "transport": "UNC localhost/C$ SMB loopback",
                "source_template": "TEMP/filmpaw-smb-evidence-*/batch/Actor/<movie>",
                "target_template": "localhost/C$/.../TEMP/filmpaw-smb-evidence-*/nas/Actor/<movie>",
                "status": task["status"], "message": task["message"],
                "movie_name": name, "total_bytes": task["total_bytes"],
                "seconds": round(time.monotonic() - start, 3),
                "source_movie_removed": not movie.exists(),
                "actor_portrait_retained": (source / "folder.jpg").read_bytes() == b"actor portrait must stay",
                "target_video_bytes": (copied / "video.mp4").stat().st_size,
                "mtime_preserved": (copied / "video.mp4").stat().st_mtime_ns == 1700000000000000000,
                "empty_directory_preserved": (copied / "empty").is_dir(),
                "nfo_preserved": (copied / "movie.nfo").read_bytes() == b"<movie>synthetic fixture</movie>",
                "subtitle_preserved": (copied / "subs" / "zh.srt").read_bytes() == b"synthetic subtitle",
                "sha256_verified_by_engine": task["status"] == "completed",
            }
            assert result["status"] == "completed", result
            assert all(result[k] for k in (
                "source_movie_removed", "actor_portrait_retained", "mtime_preserved",
                "empty_directory_preserved", "nfo_preserved", "subtitle_preserved",
            ))
            return result
        finally:
            service.close()
            conn.close()


def long_path_case():
    with tempfile.TemporaryDirectory(prefix="filmpaw-longpath-evidence-") as temporary:
        root = Path(temporary).resolve()
        assert root.parent == Path(tempfile.gettempdir()).resolve()
        assert root.name.startswith("filmpaw-longpath-evidence-")
        source, target = root / "batch" / "Actor", root / "nas" / "Actor"
        source.mkdir(parents=True)
        target.mkdir(parents=True)
        name = "MOV-LONG"
        movie = source / name
        movie.mkdir()
        extended = Path("\\\\?\\" + str(movie))
        nested = extended / ("a" * 100) / ("b" * 100) / ("c" * 80)
        nested.mkdir(parents=True)
        video = nested / "video.mp4"
        content = b"long path fixture remains intact"
        video.write_bytes(content)
        service, conn = service_for(root, source, target)
        try:
            task = execute(service, source, name)
            message = task["message"].replace(str(root), "<temporary fixture>")
            message = message.replace(repr(str(root))[1:-1], "<temporary fixture>")
            result = {
                "path_characters": len(str(video)) - 4,
                "status": task["status"], "message": message,
                "source_bytes_preserved": video.exists() and video.read_bytes() == content,
                "target_movie_exists": (target / name).exists(),
            }
            if task["status"] != "completed":
                assert task["status"] == "failed" and result["source_bytes_preserved"]
                assert not result["target_movie_exists"]
            return result
        finally:
            service.close()
            conn.close()
            # Only these explicitly created fixture entries use the extended
            # prefix. Remove them individually before TEMP's ordinary cleanup.
            if video.exists():
                video.unlink()
                nested.rmdir()
                nested.parent.rmdir()
                nested.parent.parent.rmdir()


if __name__ == "__main__":
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as key:
        long_paths = winreg.QueryValueEx(key, "LongPathsEnabled")[0]
    report = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "platform": platform.platform(), "python": platform.python_version(),
        "long_paths_enabled": long_paths, "smb": smb_case(), "long_path": long_path_case(),
        "temporary_fixtures_removed": True,
        "limitations": [
            "Loopback SMB uses the existing localhost C$ share; this is not a physical NAS test.",
            "The synthetic video is 32 MiB; multi-gigabyte video and real network disconnect remain untested.",
            "No system shares, permissions, network state, or long-path settings were changed.",
        ],
    }
    output = Path(__file__).with_suffix(".json")
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True))

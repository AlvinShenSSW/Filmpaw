"""Reproducible disposable Windows SMB/long-path checks for issue #36.

Run from the repo root:
    uv run --project app/server --locked python docs/evidence/issue-36/smb-smoke.py

Uses only a newly created TEMP tree, existing localhost C$ (no share/permission
changes), and an explicitly isolated FILMPAW_DB. Writes the adjacent JSON report.
"""

import argparse
import hashlib
import json
import math
import os
import platform
import secrets
import shutil
import tempfile
import threading
import time
import winreg
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from filmpaw_server.app import create_app
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


def smb_case(video_mib):
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
        source_hash = hashlib.sha256()
        chunk = bytes(range(256)) * 4096  # one reusable MiB, independent of file size
        generated_at = time.monotonic()
        with (movie / "video.mp4").open("wb") as output:
            for _ in range(video_mib):
                output.write(chunk)
                source_hash.update(chunk)
        generation_seconds = round(time.monotonic() - generated_at, 3)
        (movie / "movie.nfo").write_bytes(b"<movie>synthetic fixture</movie>")
        (movie / "subs" / "zh.srt").write_bytes(b"synthetic subtitle")
        os.utime(movie / "video.mp4", ns=(1700000000000000000, 1700000000000000000))
        (source / "folder.jpg").write_bytes(b"actor portrait must stay")
        service, conn = service_for(root, source, target)
        service.close()
        conn.close()
        # Exercise real ASGI routes and their session middleware while the
        # transfer runs in its normal background thread. No TCP server, real
        # library database, or external NAS is involved.
        token = secrets.token_hex(32)
        os.environ["FILMPAW_SESSION_TOKEN"] = token
        headers = {"X-Filmpaw-Session": token}
        samples = []
        start = time.monotonic()
        with TestClient(create_app(root / "fixture.db")) as client:
            context_response = client.post("/api/archive/contexts", headers=headers, json={
                "local_dir": str(source.parent), "subdir": "Actor", "performer_id": "actor",
            })
            context_response.raise_for_status()
            context = context_response.json()
            assert not context["source_error"] and not context["target_error"], context
            created = client.post("/api/archive/transfers", headers=headers, json={
                "context_id": context["id"], "names": [name], "request_id": "disposable-http-smoke",
            })
            created.raise_for_status()
            task = created.json()
            print(json.dumps({"event": "transfer_started", "video_mib": video_mib}), flush=True)
            deadline = time.monotonic() + max(180, video_mib * 0.5)
            last_phase = None
            while True:
                before_request = time.perf_counter()
                response = client.get(f"/api/archive/transfers/{task['id']}", headers=headers)
                latency_ms = (time.perf_counter() - before_request) * 1000
                response.raise_for_status()
                task = response.json()
                samples.append({
                    "elapsed_ms": round((time.monotonic() - start) * 1000, 3),
                    "latency_ms": round(latency_ms, 3), "phase": task["phase"],
                    "status": task["status"], "progress_bytes": task["progress_bytes"],
                    "verified_bytes": task["verified_bytes"],
                })
                if task["phase"] != last_phase:
                    last_phase = task["phase"]
                    print(json.dumps({"event": "phase", "phase": last_phase}), flush=True)
                if task["status"] not in {"queued", "running"}:
                    break
                if time.monotonic() > deadline:
                    client.post(f"/api/archive/transfers/{task['id']}/stop", headers=headers)
                    raise TimeoutError("SMB fixture exceeded its bounded transfer deadline")
                time.sleep(0.1)
            transfer_seconds = round(time.monotonic() - start, 3)
            assert task["status"] == "completed", task
            copied = target / name
            destination_hash = hashlib.sha256()
            with (copied / "video.mp4").open("rb") as input_file:
                while chunk := input_file.read(1024 * 1024):
                    destination_hash.update(chunk)
            latencies = sorted(sample["latency_ms"] for sample in samples)
            result = {
                "transport": "UNC localhost/C$ SMB loopback",
                "source_template": "TEMP/filmpaw-smb-evidence-*/batch/Actor/<movie>",
                "target_template": "localhost/C$/.../TEMP/filmpaw-smb-evidence-*/nas/Actor/<movie>",
                "status": task["status"], "message": task["message"],
                "movie_name": name, "total_bytes": task["total_bytes"],
                "video_mib": video_mib, "generation_seconds": generation_seconds,
                "seconds": transfer_seconds,
                "source_movie_removed": not movie.exists(),
                "actor_portrait_retained": (source / "folder.jpg").read_bytes() == b"actor portrait must stay",
                "target_video_bytes": (copied / "video.mp4").stat().st_size,
                "mtime_preserved": (copied / "video.mp4").stat().st_mtime_ns == 1700000000000000000,
                "empty_directory_preserved": (copied / "empty").is_dir(),
                "nfo_preserved": (copied / "movie.nfo").read_bytes() == b"<movie>synthetic fixture</movie>",
                "subtitle_preserved": (copied / "subs" / "zh.srt").read_bytes() == b"synthetic subtitle",
                "sha256_verified_by_engine": task["status"] == "completed",
                "independent_sha256": {
                    "source_generated_stream": source_hash.hexdigest(),
                    "destination_unc_reread": destination_hash.hexdigest(),
                    "equal": source_hash.digest() == destination_hash.digest(),
                },
                "status_api": {
                    "method": "GET /api/archive/transfers/{task_id}",
                    "transport": "FastAPI TestClient in-process HTTP/ASGI; includes real session middleware",
                    "sample_count": len(samples), "min_latency_ms": latencies[0],
                    "max_latency_ms": latencies[-1],
                    "p95_latency_ms": latencies[math.ceil(len(latencies) * 0.95) - 1],
                    "mean_latency_ms": round(sum(latencies) / len(latencies), 3),
                    "samples": samples,
                },
            }
            assert result["status"] == "completed", result
            assert all(result[k] for k in (
                "source_movie_removed", "actor_portrait_retained", "mtime_preserved",
                "empty_directory_preserved", "nfo_preserved", "subtitle_preserved",
            ))
            assert result["target_video_bytes"] == video_mib * 1024 * 1024
            assert result["independent_sha256"]["equal"]
            return result


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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video-mib", type=int, default=32)
    parser.add_argument("--output", type=Path, default=Path(__file__).with_suffix(".json"))
    args = parser.parse_args()
    if args.video_mib < 1:
        parser.error("--video-mib must be positive")
    free_bytes = shutil.disk_usage(tempfile.gettempdir()).free
    required_bytes = max((12 if args.video_mib > 2048 else 1) * 1024**3,
                         args.video_mib * 1024**2 * 2 + 512 * 1024**2)
    if free_bytes < required_bytes:
        parser.error(f"TEMP volume needs at least {required_bytes} bytes free; has {free_bytes}")
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as key:
        long_paths = winreg.QueryValueEx(key, "LongPathsEnabled")[0]
    report = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "platform": platform.platform(), "python": platform.python_version(),
        "temporary_volume_free_bytes_before": free_bytes,
        "temporary_volume_required_bytes": required_bytes,
        "long_paths_enabled": long_paths, "smb": smb_case(args.video_mib), "long_path": long_path_case(),
        "temporary_fixtures_removed": True,
        "limitations": [
            "Loopback SMB uses the existing localhost C$ share; this is not a physical NAS test.",
            f"The synthetic video is {args.video_mib} MiB; real network disconnect remains untested.",
            "Status latency measures real ASGI endpoint handling in-process, not TCP or the Tauri WebView.",
            "No system shares, permissions, network state, or long-path settings were changed.",
        ],
    }
    output = args.output.resolve()
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True))

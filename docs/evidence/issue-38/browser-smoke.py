"""Actual built UI + isolated sidecar cleanup flow; fixtures stay in TEMP."""

import hashlib
import json
import os
import queue
import secrets
import subprocess
import tempfile
import threading
import time
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[3]
EVIDENCE = Path(__file__).parent


class Frontend(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if not Path(self.translate_path(self.path.split("?")[0])).exists():
            self.path = "/index.html"
        return super().do_GET()


def main():
    fixture = Path(tempfile.mkdtemp(prefix="filmpaw-38-browser-")).resolve()
    assert fixture.parent == Path(tempfile.gettempdir()).resolve()
    assert fixture.name.startswith("filmpaw-38-browser-")
    token = secrets.token_hex(32)
    env = dict(os.environ, FILMPAW_DB=str(fixture / "smoke.db"), FILMPAW_DEV="1",
               FILMPAW_SESSION_TOKEN=token)
    stderr = (fixture / "server.log").open("w", encoding="utf-8")
    server = subprocess.Popen(
        [str(ROOT / "app/server/.venv/Scripts/python.exe"), "-m", "filmpaw_server.main"],
        cwd=ROOT / "app/server", env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=stderr, text=True, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    lines = queue.Queue()
    threading.Thread(target=lambda: lines.put(server.stdout.readline().strip()), daemon=True).start()
    httpd = None
    report = {"page_errors": [], "checks": [], "viewports": [], "fixture": str(fixture)}
    try:
        line = lines.get(timeout=30)
        assert line.startswith("FILMPAW_PORT="), line
        port = int(line.split("=")[1])

        def api(path, method="GET", body=None):
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api{path}", method=method,
                data=json.dumps(body).encode() if body is not None else None,
                headers={"Content-Type": "application/json", "X-Filmpaw-Session": token},
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.load(response)

        deadline = time.monotonic() + 20
        while True:
            try:
                report["version"] = api("/health")["version"]
                break
            except OSError:
                assert time.monotonic() < deadline, "sidecar readiness timeout"
                time.sleep(0.1)
        batch = fixture / "batch"
        actor = "林夏"
        (batch / actor).mkdir(parents=True)
        (batch / "空目录 2").mkdir()
        (batch / "保留海报").mkdir()
        (batch / "保留海报" / "folder.jpg").write_bytes(b"poster fixture")
        (batch / "保留隐藏文件").mkdir()
        (batch / "保留隐藏文件" / ".keep").write_bytes(b"")
        nas = fixture / "nas"
        target = nas / actor
        target.mkdir(parents=True)
        (target / "FILM-001").mkdir()
        video = target / "FILM-001" / "video.mp4"
        video.write_bytes(b"right library control")
        expected_hash = hashlib.sha256(video.read_bytes()).hexdigest()
        sid = api("/sources", "POST", {"unc_path": str(nas), "label": "Disposable NAS control"})["id"]
        api(f"/sources/{sid}/scan", "POST")
        api("/settings", "PUT", {"last_local_dir": str(batch)})
        httpd = ThreadingHTTPServer(("127.0.0.1", 3000), partial(Frontend, directory=str(ROOT / "app/ui/dist")))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page(viewport={"width": 1200, "height": 800})
            page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
            page.add_init_script(f"window.__FILMPAW_PORT__={port};window.__FILMPAW_SESSION_TOKEN__={json.dumps(token)};")
            posts = []
            page.on("request", lambda request: posts.append(request.url)
                    if request.method == "POST" and request.url.endswith("/api/archive/cleanups") else None)
            page.goto("http://127.0.0.1:3000")
            page.get_by_role("link", name="归档对比", exact=True).click()
            page.get_by_role("button", name=actor, exact=True).click()
            page.get_by_role("button", name=f"选择此目录：{actor} {target}").click()
            expect(page.get_by_role("region", name="NAS 片库", exact=True)).to_be_visible()
            button = page.get_by_role("button", name="清理空演员目录", exact=True)
            expect(button).to_be_enabled()
            for width, height in [(960, 640), (1200, 800), (1920, 1080), (2560, 1440)]:
                page.set_viewport_size({"width": width, "height": height})
                bounds = button.bounding_box()
                size = page.evaluate("({width:document.documentElement.scrollWidth,height:document.documentElement.scrollHeight})")
                assert size["width"] <= width and size["height"] <= height, size
                assert bounds and bounds["x"] >= 0 and bounds["y"] + bounds["height"] <= height
                report["viewports"].append({"width": width, "height": height, "cleanup_button_visible": True})
            page.set_viewport_size({"width": 1200, "height": 800})
            page.screenshot(path=str(EVIDENCE / "cleanup-ready.png"))

            # Accept the real deletion request, then lose only its HTTP response.
            def lose_response(route):
                response = route.fetch()
                assert response.status == 202, response.text()
                route.abort("failed")

            page.route("**/api/archive/cleanups", lose_response, times=1)
            button.click()
            status = page.get_by_role("status", name="空演员目录清理", exact=True)
            expect(status.get_by_text("空演员目录清理完成", exact=True)).to_be_visible(timeout=20000)
            expect(page.get_by_role("button", name=actor, exact=True)).to_have_count(0)
            expect(page.get_by_role("button", name="空目录 2", exact=True)).to_have_count(0)
            expect(page.get_by_role("button", name="保留海报", exact=True)).to_be_visible()
            expect(page.get_by_role("region", name="NAS 片库", exact=True)).to_have_count(0)
            assert len(posts) == 1, posts
            assert batch.is_dir() and not (batch / actor).exists()
            assert not (batch / "空目录 2").exists()
            assert (batch / "保留海报" / "folder.jpg").read_bytes() == b"poster fixture"
            assert (batch / "保留隐藏文件" / ".keep").exists()
            assert target.is_dir() and hashlib.sha256(video.read_bytes()).hexdigest() == expected_hash
            report["checks"].append("actual deletion recovered from lost response with one POST; strict nonempty and NAS control retained")
            page.screenshot(path=str(EVIDENCE / "cleanup-complete.png"))

            page.get_by_role("link", name="关于", exact=True).click()
            expect(status).to_be_visible()
            page.reload()
            expect(page.get_by_role("status", name="空演员目录清理", exact=True)).to_be_visible(timeout=15000)
            assert len(posts) == 1
            report["checks"].append("cleanup result survives route change and reload without replay")
            assert api("/archive/prepare-exit", "POST")["safe"] is True
            assert not report["page_errors"], report["page_errors"]
            browser.close()
        (EVIDENCE / "browser-smoke.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"report": str(EVIDENCE / "browser-smoke.json"), "checks": report["checks"]}, ensure_ascii=False))
    finally:
        if httpd:
            httpd.shutdown()
            httpd.server_close()
        if server.poll() is None:
            subprocess.run(["taskkill", "/PID", str(server.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        server.wait(timeout=10)
        stderr.close()
        print(f"Disposable browser fixture retained for inspection: {fixture}")


if __name__ == "__main__":
    main()

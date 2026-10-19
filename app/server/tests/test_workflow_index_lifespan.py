"""App ownership and actual HTTP projection, using an owned temporary library."""

import sqlite3
from contextlib import closing
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from filmpaw_server.app import create_app
from filmpaw_server.db import connect
from filmpaw_server.session import library_session
from filmpaw_server.workflow_models import WorkflowIndexConfig, WorkflowIndexError
from tests.conftest import SESSION_TOKEN
from tests.test_workflow_index_query import query


def config(library_id, roots=()):
    return WorkflowIndexConfig(component_id=str(uuid4()), library_id=library_id,
                               read_token=SecretStr("owned-fixture-read-token"), allowed_root_ids=roots)


def test_unproved_scanner_shutdown_keeps_database_and_library_lease(tmp_path, monkeypatch):
    path = tmp_path / "owned.sqlite"
    app = create_app(path)
    with pytest.raises(RuntimeError, match="扫描退出尚未确认"):
        with TestClient(app):
            # No scanner is launched. Inject only its final proof result.
            assert app.state.scans.close().clean
            monkeypatch.setattr(app.state.scans, "close", lambda: SimpleNamespace(clean=False))
    try:
        assert app.state.shutdown_pending
        assert app.state.db.execute("SELECT 1").fetchone()[0] == 1
        with pytest.raises(RuntimeError, match="另一个 Filmpaw 会话"):
            with library_session(path):
                pytest.fail("unproved shutdown must retain its library lease")
    finally:
        # This fixture has no native child or I/O; release only its own resources.
        app.state.cleanups.close()
        app.state.transfers.close()
        app.state.db.close()
        app.state.library_session.__exit__(None, None, None)
        app.state.library_session = None
    with library_session(path):
        pass


def test_startup_library_mismatch_releases_unstarted_session(tmp_path):
    path = tmp_path / "owned.sqlite"
    app = create_app(path, workflow_index=config(str(uuid4())))
    with pytest.raises(WorkflowIndexError, match="library_mismatch"):
        with TestClient(app):
            pytest.fail("mismatched configuration must not serve requests")
    assert app.state.library_session is None
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        app.state.db.execute("SELECT 1")
    with library_session(path):
        pass


def test_workflow_routes_are_disabled_without_explicit_configuration(tmp_path):
    with TestClient(create_app(tmp_path / "owned.sqlite")) as client:
        assert client.get("/api/workflow/v1/index").status_code == 404
        assert client.post("/api/workflow/v1/candidates/query", json={}).status_code == 404
        assert not client.app.state.scans._auto_refresh


def test_real_index_roundtrip_is_read_only_and_preserves_coverage(tmp_path):
    path, root = tmp_path / "owned.sqlite", tmp_path / "owned-root"
    (root / "Example Actor" / "Example Film").mkdir(parents=True)
    with TestClient(create_app(path), headers={"X-Filmpaw-Session": SESSION_TOKEN}) as legacy:
        source = legacy.post("/api/sources", json={"unc_path": str(root)})
        assert source.status_code == 201, source.text
        scanned = legacy.post(f"/api/sources/{source.json()['id']}/scan")
        assert scanned.status_code == 200, scanned.text
        index = legacy.app.state.workflow_index
        root_id = index.root_for_source(source.json()["id"])
        cfg = config(index.library_id, (root_id,))
    with TestClient(create_app(path, workflow_index=cfg)) as client:
        headers = {"Authorization": "Bearer owned-fixture-read-token", "Content-Type": "application/json"}
        index = client.app.state.workflow_index
        with index.lock:
            before = index.conn.total_changes
            operations = index.conn.execute("SELECT count(*) FROM workflow_scan_operations").fetchone()[0]
        response = client.post("/api/workflow/v1/candidates/query", content=query((root_id,)).model_dump_json(), headers=headers)
        assert response.status_code == 200, response.text
        page = response.json()
        assert page["match_state"] == "matched" and page["coverage_state"] == "complete"
        assert page["total"] == 1 and page["candidates"][0]["actor_display_name"] == "Example Actor"
        assert str(root) not in response.text and "owned-fixture-read-token" not in response.text
        overview = client.get("/api/workflow/v1/index", headers=headers)
        assert overview.status_code == 200, overview.text
        with index.lock:
            assert index.conn.total_changes == before
            assert index.conn.execute("SELECT count(*) FROM workflow_scan_operations").fetchone()[0] == operations
    with closing(connect(path)) as db:
        assert db.execute("SELECT count(*) FROM performers").fetchone()[0] == 1

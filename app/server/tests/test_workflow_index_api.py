"""HTTP authority and wire checks; the index itself has separate state tests."""

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

import filmpaw_server.app as app_module
from filmpaw_server.workflow_models import (
    ActorMatch, CandidatePage, IndexOverview, RootCoverage,
    WorkflowIndexConfig, digest,
)


READ_TOKEN = "fixture-workflow-reader-" + "b" * 40
NOW = datetime(2026, 10, 19, tzinfo=timezone.utc)


@pytest.fixture()
def wire(tmp_path, monkeypatch):
    root_id, host_id, export_id = (str(uuid4()) for _ in range(3))
    config = WorkflowIndexConfig(
        component_id=str(uuid4()), library_id=str(uuid4()),
        read_token=SecretStr(READ_TOKEN), allowed_root_ids=(root_id,),
    )
    coverage = RootCoverage(
        root_id=root_id, host_id=host_id, export_id=export_id,
        host_label="Fixture NAS", root_label="Fixture library", root_revision=1,
        health="never_scanned", coverage="incomplete", index_revision=1,
    )
    calls = []

    class QueryIndex:
        library_id = config.library_id

        def __init__(self, *_args, **_kwargs):
            pass

        def overview(self, supplied):
            assert supplied == config
            calls.append("overview")
            return IndexOverview(
                library_id=config.library_id, component_id=config.component_id,
                index_revision=1, generated_at=NOW, roots=(coverage,),
            )

        def query(self, body, supplied):
            assert supplied == config
            calls.append(body)
            return CandidatePage(
                library_id=config.library_id, component_id=config.component_id,
                index_revision=1, query_hash=digest(body), generated_at=NOW,
                roots=(coverage,), actor_results=tuple(ActorMatch(
                    actor_ref=actor.actor_ref, match_state="ambiguous",
                    coverage_state="incomplete", total=0, reasons=("coverage_gap",),
                ) for actor in body.actors), candidates=(), total=0,
                coverage_state="incomplete", match_state="ambiguous",
                reasons=("coverage_gap",),
            )

    class IdleScanner:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self, **_kwargs):
            pass

        def close(self):
            return SimpleNamespace(clean=True)

    monkeypatch.setattr(app_module, "WorkflowIndex", QueryIndex, raising=False)
    monkeypatch.setattr(app_module, "ScanSupervisor", IdleScanner, raising=False)
    app = app_module.create_app(tmp_path / "wire.db", workflow_index=config)
    body = {
        "schema_version": 1, "request_id": str(uuid4()), "film_id": str(uuid4()),
        "metadata_revision": 1, "final_revision": 1, "final_hash": "a" * 64,
        "actors": [{"actor_ref": str(uuid4()), "name": "Fixture actor", "aliases": [], "external_ids": []}],
        "film": {"title": "Fixture film", "edition_hints": []},
        "root_ids": [root_id], "proposed_film_relative_path": "FIXTURE-001", "limit": 20,
    }
    with TestClient(app) as client:
        yield client, body, calls


def read_headers():
    return {"Authorization": f"Bearer {READ_TOKEN}"}


@pytest.mark.parametrize("headers", [{}, {"X-Filmpaw-Session": "a" * 64}, {"Authorization": "Bearer wrong-secret"}])
def test_workflow_reads_require_the_independent_token(wire, headers):
    client, body, calls = wire
    assert client.get("/api/workflow/v1/index", headers=headers).status_code == 401
    assert client.post("/api/workflow/v1/candidates/query", headers=headers, json=body).status_code == 401
    assert calls == []


def test_query_accepts_json_arrays_and_returns_only_public_projection(wire):
    client, body, calls = wire
    response = client.post("/api/workflow/v1/candidates/query", headers=read_headers(), json=body)
    assert response.status_code == 200, response.text
    assert response.json()["match_state"] == "ambiguous"
    assert response.json()["coverage_state"] == "incomplete"
    assert len(calls) == 1 and calls[0].actors[0].name == "Fixture actor"
    assert READ_TOKEN not in response.text
    assert "private_root_path" not in response.text
    assert client.get("/api/workflow/v1/index", headers=read_headers()).status_code == 200


@pytest.mark.parametrize("path", ["/api/sources", "/api/scan-all", "/api/archive/prepare-exit", "/api/settings"])
def test_readonly_token_cannot_authorize_legacy_mutations(wire, path):
    client, _, calls = wire
    response = client.post(path, headers=read_headers(), json={})
    assert response.status_code == 403
    assert calls == []


@pytest.mark.parametrize("change", [
    {"metadata_revision": True}, {"limit": 201}, {"schema_version": 2},
    {"proposed_film_relative_path": "../fixture-secret"},
    {"private_path": "C:/fixture-secret"},
])
def test_invalid_query_does_not_echo_input(wire, change):
    client, body, calls = wire
    response = client.post("/api/workflow/v1/candidates/query", headers=read_headers(), json={**body, **change})
    assert response.status_code == 422
    assert set(response.json()) == {"code", "request_id"}
    assert "fixture-secret" not in response.text and READ_TOKEN not in response.text
    assert calls == []


def test_streaming_body_is_bounded_without_content_length(wire):
    client, _, calls = wire
    response = client.post("/api/workflow/v1/candidates/query", headers={**read_headers(), "Content-Type": "application/json"},
                           content=(b"x" * 10000 for _ in range(8)))
    assert response.status_code == 413
    assert response.json()["code"] == "body_too_large"
    assert calls == []


def test_allowed_roots_are_checked_before_querying_the_library(wire):
    client, body, calls = wire
    response = client.post("/api/workflow/v1/candidates/query", headers=read_headers(),
                           json={**body, "root_ids": [str(uuid4())]})
    assert response.status_code == 403
    assert calls == []


def test_reader_credential_is_not_accepted_on_legacy_private_reads(wire):
    client, _, _ = wire
    assert client.get("/api/settings", headers=read_headers()).status_code == 403


@pytest.mark.parametrize("content", [b"{not-json", b"[" * 3000 + b"]" * 3000])
def test_malformed_and_deep_json_have_sanitized_errors(wire, content):
    client, _, calls = wire
    response = client.post("/api/workflow/v1/candidates/query", content=content,
                           headers={**read_headers(), "Content-Type": "application/json"})
    assert response.status_code == 422
    assert set(response.json()) == {"code", "request_id"}
    assert calls == []


def test_preflight_uses_only_the_read_scope_and_fixed_errors(wire):
    client, _, calls = wire
    allowed = {"Origin": "http://tauri.localhost", "Access-Control-Request-Method": "POST",
               "Access-Control-Request-Headers": "Authorization, Content-Type"}
    result = client.options("/api/workflow/v1/candidates/query", headers=allowed)
    assert result.status_code == 200
    assert result.headers["access-control-allow-origin"] == allowed["Origin"]
    denied = client.options("/api/workflow/v1/candidates/query", headers={**allowed, "Access-Control-Request-Method": "DELETE"})
    assert denied.status_code == 400
    assert set(denied.json()) == {"code", "request_id"}
    assert calls == []


def test_duplicate_credentials_and_foreign_origin_are_rejected(wire):
    client, body, calls = wire
    duplicate = [("Authorization", f"Bearer {READ_TOKEN}")] * 2
    assert client.post("/api/workflow/v1/candidates/query", headers=duplicate, json=body).status_code == 401
    foreign = {**read_headers(), "Origin": "https://fixture.invalid"}
    assert client.post("/api/workflow/v1/candidates/query", headers=foreign, json=body).status_code == 403
    assert calls == []


def test_openapi_describes_the_same_typed_query(wire):
    client, _, _ = wire
    spec = client.get("/openapi.json").json()
    query = spec["paths"]["/api/workflow/v1/candidates/query"]["post"]["requestBody"]
    assert query["content"]["application/json"]["schema"]["$ref"] == "#/components/schemas/CandidateQuery"
    assert spec["components"]["schemas"]["CandidateQuery"]["properties"]["actors"]["type"] == "array"

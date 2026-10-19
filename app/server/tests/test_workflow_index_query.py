from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import SecretStr

from filmpaw_server.workflow_models import (
    ActorQuery, CandidateQuery, ExternalID, FilmObservation, FilmQuery, MetadataEvidence,
    WorkflowIndexConfig, WorkflowIndexError, canonical_json,
)
from tests.test_workflow_index_state import authorize_request, h, open_circuit  # noqa: F401 -- scoped SQLite fixture


def config(h, roots):
    return WorkflowIndexConfig(component_id=str(uuid4()), library_id=h.index.library_id,
                               read_token=SecretStr("synthetic-test-token"), allowed_root_ids=tuple(roots))


def query(roots, name="Example Actor", **kw):
    return CandidateQuery(request_id=str(uuid4()), film_id=str(uuid4()), metadata_revision=1,
                          final_revision=1, final_hash="a" * 64,
                          actors=(ActorQuery(actor_ref=str(uuid4()), name=name),), root_ids=tuple(roots),
                          proposed_film_relative_path="Example Film", **kw)


def test_probe_only_zero_candidates_are_ambiguous_and_no_private_mapping_leaks(h):
    root, _ = h.root(path="//private-example/secret-share")
    result = h.index.query(query((root,)), config(h, (root,)))
    assert result.match_state == "ambiguous" and result.coverage_state == "incomplete"
    assert result.total == 0 and result.reasons == ("coverage_gap",)
    body = canonical_json(result)
    assert "private-example" not in body and "secret-share" not in body and "read_token" not in body


def test_full_scan_no_match_and_stale_zero_are_distinct(h):
    root, _ = h.root()
    req, _, outcome = h.scan(root)
    h.index.finish(req, outcome)
    q, cfg = query((root,), "Nobody"), config(h, (root,))
    assert h.index.query(q, cfg).match_state == "no_match"
    h.now += timedelta(days=2)
    page = h.index.query(q, cfg)
    assert page.match_state == "ambiguous" and page.roots[0].health == "stale"


@pytest.mark.parametrize("names", [(), ("Example Actor A", "Example Actor B")])
def test_restart_unknown_owner_invalidates_coverage_and_cursor_without_losing_cache(h, names):
    from filmpaw_server.workflow_index import WorkflowIndex

    root, source = h.root()
    unaffected, other_source = h.root()
    for rid, actors in ((root, names), (unaffected, ())):
        req, _, outcome = h.scan(rid, actors)
        assert h.index.finish(req, outcome).published
    cfg = config(h, (root, unaffected))
    absent = query((root, unaffected), "Nobody")
    matching = query((root,), "Example Actor", limit=1)
    matching_cfg = config(h, (root,))
    before = h.index.query(absent, cfg)
    cached = h.index.query(matching, matching_cfg)
    success = h.index.source_health((source, other_source))
    req, owner, _ = h.scan(root)

    reopened = WorkflowIndex(h.db, h.lock, clock=lambda: h.now)
    after = reopened.query(absent, cfg)
    health = reopened.source_health((source, other_source))
    assert after.coverage_state == "incomplete" and after.match_state == "ambiguous"
    assert after.reasons == ("coverage_gap",)
    assert after.index_revision > before.index_revision
    assert health[source].health == "unknown_owner"
    assert health[source].last_success_at == success[source].last_success_at
    assert health[other_source].health == "fresh"
    assert health[other_source].last_success_at == success[other_source].last_success_at
    pending = reopened.pending_owners()
    assert len(pending) == 1 and pending[0].state == "unknown_owner"
    assert pending[0].request == req and pending[0].owner == owner
    retained = reopened.query(matching, matching_cfg)
    assert retained.total == cached.total
    assert [c.location_id for c in retained.candidates] == [c.location_id for c in cached.candidates]
    if cached.next_cursor:
        with pytest.raises(WorkflowIndexError, match="index_changed"):
            reopened.query(matching.model_copy(update={"cursor": cached.next_cursor}), matching_cfg)
    with pytest.raises(WorkflowIndexError, match="busy"):
        reopened.begin_attempt(reopened.reserve_scan(unaffected, request_id=str(uuid4())))

    repeated = WorkflowIndex(h.db, h.lock, clock=lambda: h.now)
    assert repeated.query(absent, cfg).index_revision == after.index_revision
    assert repeated.source_health((source,))[source].health == "unknown_owner"
    assert repeated.pending_owners() == pending


def test_clean_restart_preserves_complete_index_revision(h):
    from filmpaw_server.workflow_index import WorkflowIndex

    root, _ = h.root()
    req, _, outcome = h.scan(root)
    assert h.index.finish(req, outcome).published
    q, cfg = query((root,)), config(h, (root,))
    before = h.index.query(q, cfg)
    reopened = WorkflowIndex(h.db, h.lock, clock=lambda: h.now)
    assert reopened.query(q, cfg) == before
    assert not reopened.pending_owners()


def test_recovery_probe_and_cancelled_full_cannot_publish_no_match(h):
    root, _ = h.root()
    open_circuit(h, root)
    full = h.index.reserve_scan(root, request_id=str(uuid4()))
    probe = h.index.start_recovery_probe(full)
    owner = authorize_request(h, probe)
    assert h.index.finish(probe, h.outcome(probe, owner)).published
    q, cfg = query((root,), "Nobody"), config(h, (root,))
    assert h.index.query(q, cfg).match_state == "ambiguous"
    req, owner = h.start(full)
    cancelled = h.index.cancel_publication(full, reason="timeout")
    assert not cancelled.published
    assert not h.index.finish(req, h.outcome(req, owner)).published
    page = h.index.query(q, cfg)
    assert page.match_state == "ambiguous" and page.coverage_state == "incomplete"


def test_same_name_multiple_roots_keeps_locations_and_all_actor_references(h):
    roots = [h.root()[0] for _ in range(2)]
    for root in roots:
        req, _, outcome = h.scan(root)
        h.index.finish(req, outcome)
    q = query(roots)
    unknown = ActorQuery(actor_ref=str(uuid4()), name="Unknown Actor")
    q = q.model_copy(update={"actors": (*q.actors, unknown), "limit": 1})
    cfg = config(h, roots)
    page = h.index.query(q, cfg)
    assert page.total == 2 and len(page.candidates) == 1 and page.next_cursor
    assert page.actor_results[0].match_state == "ambiguous"
    assert page.actor_results[1].match_state == "no_match"
    second = h.index.query(q.model_copy(update={"cursor": page.next_cursor}), cfg)
    assert second.candidates[0].location_id != page.candidates[0].location_id
    with h.lock, h.db:
        h.index.aliases_changed_tx("example actor")
    with pytest.raises(WorkflowIndexError, match="index_changed"):
        h.index.query(q.model_copy(update={"cursor": page.next_cursor}), cfg)


def test_alias_group_and_explicit_external_id_conflict_are_explained(h):
    root, _ = h.root()
    req, _, outcome = h.scan(root, ("倉木華",))
    evidence = MetadataEvidence(evidence_id=str(uuid4()), kind="explicit_external_id", value="actor-1",
                                namespace="example.test", relative_metadata_path="倉木華/Film/movie.nfo",
                                content_digest="b" * 64, observed_at=h.now)
    actor = outcome.snapshot.actors[0].model_copy(update={"evidence": (evidence,)})
    h.index.finish(req, outcome.model_copy(update={"snapshot": outcome.snapshot.model_copy(update={"actors": (actor,)})}))
    with h.lock, h.db:
        h.db.execute("INSERT INTO aliases(name_norm,alias,alias_norm) VALUES('仓木华','Example Alias','example alias')")
        h.index.aliases_changed_tx("仓木华")
    q = query((root,), "Example Alias")
    q = q.model_copy(update={"actors": (q.actors[0].model_copy(update={"external_ids": (ExternalID(namespace="example.test", value="actor-2", provenance_digest="c" * 64),)}),)})
    page = h.index.query(q, config(h, (root,)))
    assert page.match_state == "ambiguous"
    assert {"group_alias", "external_id_conflict"} <= set(page.candidates[0].match_reasons)
    assert page.candidates[0].external_id_evidence[0] == evidence


def test_film_collision_never_claims_exact_duplicate(h):
    root, _ = h.root()
    req, _, outcome = h.scan(root)
    film = FilmObservation(relative_actor_path="Example Actor", relative_film_path="Example Actor/EX-001",
                           code="EX-001", title="Example Film", edition_hints=("original",))
    h.index.finish(req, outcome.model_copy(update={"snapshot": outcome.snapshot.model_copy(update={"films": (film,)})}))
    q = query((root,), film=FilmQuery(code="EX-001", edition_hints=("restored",)))
    conflict = h.index.query(q, config(h, (root,))).candidates[0].film_conflicts[0]
    assert conflict.reasons == ("possible_same_code", "version_hint_differs")


def test_query_binding_hash_stable_across_request_time_but_changes_with_metadata(h):
    root, _ = h.root()
    req, _, outcome = h.scan(root)
    h.index.finish(req, outcome)
    q, cfg = query((root,)), config(h, (root,))
    a = h.index.query(q, cfg)
    h.now += timedelta(seconds=1)
    b = h.index.query(q.model_copy(update={"request_id": str(uuid4())}), cfg)
    assert a.query_hash == b.query_hash and a.candidates[0].candidate_digest == b.candidates[0].candidate_digest
    c = h.index.query(q.model_copy(update={"metadata_revision": 2}), cfg)
    assert a.candidates[0].candidate_digest != c.candidates[0].candidate_digest


def test_allowed_unknown_roots_and_read_only_query(h):
    root, _ = h.root()
    unknown = str(uuid4())
    with pytest.raises(WorkflowIndexError) as caught:
        h.index.query(query((unknown,)), config(h, (root,)))
    assert caught.value.code == "root_forbidden"
    with pytest.raises(WorkflowIndexError) as caught:
        h.index.query(query((unknown,)), config(h, (unknown,)))
    assert caught.value.code == "unknown_root"
    before = h.db.total_changes
    h.index.query(query((root,)), config(h, (root,)))
    assert h.db.total_changes == before

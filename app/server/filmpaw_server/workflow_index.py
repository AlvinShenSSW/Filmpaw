"""F01 single-owner repository and read-only candidate projection.

No filesystem probing or subprocess waits occur here. The owner supplies the
same SQLite connection/RLock as legacy routes; each mutation is one short TX.
"""

import base64
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4, uuid5, NAMESPACE_URL

from filmpaw_server.normalize import bidirectional_match, normalize
from filmpaw_server.scan import apply_snapshot
from filmpaw_server.workflow_models import (
    ActorMatch, CandidatePage, CandidateQuery, CachedThumbnail, FilmConflict, FilmObservation,
    IdentityObservation, IndexOverview, LocationCandidate, MetadataEvidence, PendingOwner,
    ProbeDraft, ProcessOwner, RootCoverage, ScanFinish, ScanOutcome, ScanProfile, ScanRequest,
    ScanTicket, WorkflowIndexConfig, WorkflowIndexError, canonical_json, digest,
)
from filmpaw_server.workflow_schema import UNKNOWN_HOST_ID, ensure_legacy_roots


def _now():
    return datetime.now(timezone.utc)


def _time(value):
    return datetime.fromisoformat(value) if value else None


def _identity(value):
    return IdentityObservation.model_validate_json(value) if value else None


def _same_identity(a, b):
    if a is None or b is None:
        return False
    return bool({digest(v) for v in (a.identity, *a.aliases)} & {digest(v) for v in (b.identity, *b.aliases)})


class WorkflowIndex:
    def __init__(self, conn, lock, profile=None, clock=None):
        self.conn, self.lock = conn, lock
        self.profile = profile or ScanProfile()
        self.clock = clock or _now
        with self._tx():
            ensure_legacy_roots(conn)
            # A fresh repository has no old native HANDLEs. Persist uncertainty,
            # retaining every birth/Job observation for explicit reconciliation.
            conn.execute("UPDATE workflow_roots SET health='unknown_owner' WHERE tombstoned=0 "
                         "AND root_id IN (SELECT root_id FROM workflow_scan_operations WHERE owner_state='owned')")
            isolated = conn.execute("UPDATE workflow_scan_operations SET owner_state='unknown_owner',state='unknown' "
                                    "WHERE owner_state='owned'")
            if isolated.rowcount:
                self._bump()
        self.library_id = self.conn.execute("SELECT library_id FROM workflow_index_meta").fetchone()[0]

    @contextmanager
    def _tx(self):
        with self.lock:
            if self.conn.in_transaction:
                raise WorkflowIndexError("transaction_busy")
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    def _one(self, sql, args=(), code="not_found"):
        row = self.conn.execute(sql, args).fetchone()
        if row is None:
            raise WorkflowIndexError(code, 404)
        return row

    def _revision(self):
        return self.conn.execute("SELECT index_revision FROM workflow_index_meta").fetchone()[0]

    def _bump(self):
        self.conn.execute("UPDATE workflow_index_meta SET index_revision=index_revision+1")

    def root_for_source(self, source_id):
        with self.lock:
            return self._one("SELECT root_id FROM workflow_roots WHERE legacy_source_id=? AND active=1 AND tombstoned=0",
                             (source_id,))[0]

    def update_root_mapping(self, root_id, *, expected_revision, draft: ProbeDraft):
        """Local configuration CAS; never moves an existing attempt's owner.

        No HTTP worker can invoke this method. Unconfirmed host aliases use the
        shared restricted host; an explicit host ID is operator-provided fact.
        """
        with self._tx():
            root = self._one("SELECT * FROM workflow_roots WHERE root_id=? AND tombstoned=0", (root_id,))
            if type(expected_revision) is not int or root["root_revision"] != expected_revision:
                raise WorkflowIndexError("revision_conflict")
            collision = self.conn.execute("SELECT 1 FROM workflow_roots WHERE root_id!=? AND private_path=? AND tombstoned=0", (root_id, draft.private_root_path)).fetchone()
            if collision:
                raise WorkflowIndexError("source_exists")
            host = draft.host_id or UNKNOWN_HOST_ID
            self.conn.execute("INSERT OR IGNORE INTO workflow_host_state(host_id,host_label) VALUES(?,?)", (host, draft.host_label))
            self.conn.execute("UPDATE workflow_roots SET host_id=?,export_id=?,private_path=?,label=?,root_revision=root_revision+1,mapping_revision=mapping_revision+1,identity_json=NULL,health='never_scanned',last_success_at=NULL,observed_at=NULL,probe_observed_at=NULL,probe_health='never_scanned' WHERE root_id=?",
                              (host, draft.export_id or root["export_id"], draft.private_root_path, draft.label, root_id))
            if root["legacy_source_id"] is not None:
                self.conn.execute("UPDATE sources SET unc_path=?,label=?,last_scan_at=NULL WHERE id=?", (draft.private_root_path, draft.label, root["legacy_source_id"]))
            self.conn.execute("UPDATE workflow_locations SET state='unknown',revision=revision+1 WHERE root_id=? AND retired=0", (root_id,))
            self.conn.execute("UPDATE workflow_host_state SET configuration_required=0 WHERE host_id=?", (host,))
            self._bump()
            return expected_revision + 1

    def _ticket(self, row):
        return ScanTicket(scan_id=row["scan_id"], attempt_id=row["attempt_id"], root_id=row["root_id"],
                          host_id=row["host_id"], kind=row["kind"])

    def _reserve(self, root, request_id, kind):
        existing = self.conn.execute("SELECT * FROM workflow_scan_operations WHERE request_id=?", (request_id,)).fetchone()
        if existing:
            if existing["root_id"] != root["root_id"] or existing["kind"] != kind:
                raise WorkflowIndexError("request_conflict")
            return self._ticket(existing)
        existing = self.conn.execute("SELECT * FROM workflow_scan_operations WHERE root_id=? AND kind=? AND "
                                     "(state='queued' OR owner_state IN ('owned','unknown_owner'))", (root["root_id"], kind)).fetchone()
        if existing:
            return self._ticket(existing)
        if self.conn.execute("SELECT 1 FROM workflow_scan_operations WHERE root_id=? AND state='queued'", (root["root_id"],)).fetchone():
            raise WorkflowIndexError("busy")
        queued = self.conn.execute("SELECT count(*) FROM workflow_scan_operations WHERE state='queued'").fetchone()[0]
        if queued >= self.profile.queue_limit:
            raise WorkflowIndexError("busy")
        ticket = ScanTicket(scan_id=str(uuid4()), attempt_id=str(uuid4()), root_id=root["root_id"],
                            host_id=root["host_id"], kind=kind)
        self.conn.execute("INSERT INTO workflow_scan_operations(scan_id,request_id,attempt_id,root_id,host_id,"
                          "root_revision,mapping_revision,kind,state,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (ticket.scan_id, request_id, ticket.attempt_id, root["root_id"], root["host_id"],
                           root["root_revision"], root["mapping_revision"], kind, "queued", self.clock().isoformat()))
        return ticket

    def reserve_scan(self, root_id, *, request_id, kind="scan_index"):
        if kind not in {"scan_index", "probe_root"}:
            raise WorkflowIndexError("invalid_kind", 422)
        with self._tx():
            root = self._one("SELECT * FROM workflow_roots WHERE root_id=? AND active=1 AND tombstoned=0", (root_id,))
            return self._reserve(root, request_id, kind)

    def reserve_probe(self, draft: ProbeDraft, *, request_id):
        with self._tx():
            existing = self.conn.execute("SELECT * FROM workflow_scan_operations WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                root = self._one("SELECT * FROM workflow_roots WHERE root_id=?", (existing["root_id"],))
                if root["private_path"] != draft.private_root_path or existing["kind"] != "probe_root":
                    raise WorkflowIndexError("request_conflict")
                return self._ticket(existing)
            root = self.conn.execute("SELECT * FROM workflow_roots WHERE private_path=? AND tombstoned=0", (draft.private_root_path,)).fetchone()
            if root and root["active"]:
                raise WorkflowIndexError("source_exists")
            if root is None:
                host = draft.host_id or UNKNOWN_HOST_ID
                self.conn.execute("INSERT OR IGNORE INTO workflow_host_state(host_id,host_label) VALUES(?,?)", (host, draft.host_label))
                rid = str(uuid4())
                self.conn.execute("INSERT INTO workflow_roots(root_id,host_id,export_id,private_path,label) VALUES(?,?,?,?,?)",
                                  (rid, host, draft.export_id or str(uuid4()), draft.private_root_path, draft.label))
                root = self._one("SELECT * FROM workflow_roots WHERE root_id=?", (rid,))
            return self._reserve(root, request_id, "probe_root")

    def begin_attempt(self, ticket: ScanTicket):
        with self._tx():
            row, root, host = self._queued_admission(ticket)
            if host["circuit"] == "open" and row["kind"] != "probe_root":
                raise WorkflowIndexError("probe_required")
            return self._start_attempt(row, root, host)

    def start_recovery_probe(self, full_ticket: ScanTicket) -> ScanRequest:
        """Start a bounded half-open probe without replacing the full waiter.

        The full operation keeps the sole queued slot for this root. This probe
        gets independent IDs and an owner immediately in the same transaction;
        repeated calls cannot return an already-owned request for a second spawn.
        """
        with self._tx():
            row, root, host = self._queued_admission(full_ticket)
            if row["kind"] != "scan_index" or host["circuit"] != "open":
                raise WorkflowIndexError("probe_not_required")
            scan_id, attempt_id = str(uuid4()), str(uuid4())
            self.conn.execute("INSERT INTO workflow_scan_operations(scan_id,request_id,attempt_id,root_id,host_id,"
                              "root_revision,mapping_revision,kind,state,owner_state,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                              (scan_id, str(uuid4()), attempt_id, root["root_id"], root["host_id"],
                               root["root_revision"], root["mapping_revision"], "probe_root", "starting", "owned", self.clock().isoformat()))
            probe = self._one("SELECT * FROM workflow_scan_operations WHERE scan_id=?", (scan_id,))
            return self._start_attempt(probe, root, host)

    def _queued_admission(self, ticket):
        row = self._one("SELECT * FROM workflow_scan_operations WHERE scan_id=? AND attempt_id=?",
                        (ticket.scan_id, ticket.attempt_id))
        if row["state"] != "queued" or not row["publication_allowed"]:
            raise WorkflowIndexError("attempt_already_started")
        root = self._one("SELECT * FROM workflow_roots WHERE root_id=?", (row["root_id"],))
        if root["tombstoned"] or root["root_revision"] != row["root_revision"] or root["mapping_revision"] != row["mapping_revision"]:
            raise WorkflowIndexError("stale_binding")
        count = self.conn.execute("SELECT count(*) FROM workflow_scan_operations WHERE owner_state IN ('owned','unknown_owner')").fetchone()[0]
        host_busy = self.conn.execute("SELECT 1 FROM workflow_scan_operations WHERE host_id=? AND owner_state IN ('owned','unknown_owner')", (root["host_id"],)).fetchone()
        if count >= self.profile.max_owners or host_busy:
            raise WorkflowIndexError("busy")
        host = self._one("SELECT * FROM workflow_host_state WHERE host_id=?", (root["host_id"],))
        if host["configuration_required"]:
            raise WorkflowIndexError("configuration_required")
        if host["next_probe_at"] and self.clock() < _time(host["next_probe_at"]):
            raise WorkflowIndexError("backoff")
        return row, root, host

    def _start_attempt(self, row, root, host):
        generation = host["generation"] + 1
        cached = tuple(CachedThumbnail(relative_actor_path=r["folder_name"], mtime=r["thumb_mtime"], side=r["thumb_side"])
                       for r in self.conn.execute("SELECT folder_name,thumb_mtime,thumb_side FROM performers WHERE source_id=?", (root["legacy_source_id"],)))
        request = ScanRequest(scan_id=row["scan_id"], attempt_id=row["attempt_id"], root_id=root["root_id"],
                              host_id=root["host_id"], export_id=root["export_id"], root_revision=root["root_revision"],
                              mapping_revision=root["mapping_revision"], owner_generation=generation, kind=row["kind"],
                              private_root_path=root["private_path"], expected_root_identity=_identity(root["identity_json"]),
                              cached_thumbnails=cached, profile=self.profile)
        self.conn.execute("UPDATE workflow_host_state SET generation=?,circuit=CASE WHEN circuit='open' THEN 'half_open' ELSE circuit END WHERE host_id=?", (generation, root["host_id"]))
        self.conn.execute("UPDATE workflow_scan_operations SET generation=?,state='starting',owner_state='owned',request_json=? WHERE scan_id=?",
                          (generation, canonical_json(request), row["scan_id"]))
        self.conn.execute("UPDATE workflow_roots SET last_attempt_at=? WHERE root_id=?", (self.clock().isoformat(), root["root_id"]))
        return request

    def _bound(self, request):
        row = self._one("SELECT * FROM workflow_scan_operations WHERE scan_id=?", (request.scan_id,))
        if row["attempt_id"] != request.attempt_id or row["generation"] != request.owner_generation or row["request_json"] != canonical_json(request):
            raise WorkflowIndexError("stale_binding")
        return row

    def authorize_go(self, request: ScanRequest, owner: ProcessOwner):
        if owner.worker is None or owner.identity_uncertain:
            raise WorkflowIndexError("worker_unverified")
        with self._tx():
            row = self._bound(request)
            owner = self._merge_owner(row["owner_json"], owner)
            if owner.identity_uncertain or owner.unresolved_member_pids:
                raise WorkflowIndexError("worker_unverified")
            root = self._one("SELECT * FROM workflow_roots WHERE root_id=?", (request.root_id,))
            if row["state"] != "starting" or not row["publication_allowed"] or root["tombstoned"] or root["root_revision"] != request.root_revision:
                raise WorkflowIndexError("stale_binding")
            self.conn.execute("UPDATE workflow_scan_operations SET state='running',owner_json=? WHERE scan_id=?", (canonical_json(owner), request.scan_id))

    @staticmethod
    def _merge_owner(old_json, owner):
        if not old_json:
            return owner
        old = ProcessOwner.model_validate_json(old_json)
        if (old.runner_instance_id != owner.runner_instance_id or old.job_name != owner.job_name
                or old.launcher != owner.launcher or (old.worker is not None and old.worker != owner.worker)):
            raise WorkflowIndexError("owner_changed")
        members = {canonical_json(b): b for b in (*old.members, *owner.members)}
        return owner.model_copy(update={"members": tuple(members.values()),
            "identity_uncertain": old.identity_uncertain or owner.identity_uncertain,
            "unresolved_member_pids": tuple(sorted(set(old.unresolved_member_pids + owner.unresolved_member_pids)))})

    def update_owner(self, request, owner):
        with self._tx():
            row = self._bound(request)
            if row["owner_state"] == "released":
                raise WorkflowIndexError("owner_released")
            owner = self._merge_owner(row["owner_json"], owner)
            self.conn.execute("UPDATE workflow_scan_operations SET owner_json=? WHERE scan_id=?", (canonical_json(owner), request.scan_id))

    def cancel_publication(self, ticket, *, reason="cancelled") -> ScanFinish:
        """Fence future publication, or return an already committed success.

        Consumers use this transaction's result after a wait deadline; a finish
        that won the lock must not be reported as a cancelled, retained index.
        Existing running/unknown owners remain owned until proven settled.
        """
        with self._tx():
            row = self._one("SELECT * FROM workflow_scan_operations WHERE scan_id=? AND attempt_id=?", (ticket.scan_id, ticket.attempt_id))
            if row["finish_json"]:
                result = ScanFinish.model_validate_json(row["finish_json"])
                if result.published:
                    return result
            self.conn.execute("UPDATE workflow_scan_operations SET publication_allowed=0 WHERE scan_id=?", (ticket.scan_id,))
            if row["state"] == "queued":
                result = ScanFinish(scan_id=ticket.scan_id, attempt_id=ticket.attempt_id, status="cancelled", owner_state="none", error_code=reason)
                self.conn.execute("UPDATE workflow_scan_operations SET state='cancelled',finish_json=? WHERE scan_id=?", (canonical_json(result), ticket.scan_id))
            return self.get_scan(ticket.scan_id)

    def pending_owners(self):
        with self.lock:
            return tuple(PendingOwner(request=ScanRequest.model_validate_json(r["request_json"]),
                                      owner=ProcessOwner.model_validate_json(r["owner_json"]) if r["owner_json"] else None,
                                      state="unknown_owner" if r["owner_state"] == "unknown_owner" else r["state"])
                         for r in self.conn.execute("SELECT * FROM workflow_scan_operations WHERE owner_state IN ('owned','unknown_owner') ORDER BY created_at"))

    def queued_tickets(self):
        with self.lock:
            return tuple(self._ticket(r) for r in self.conn.execute("SELECT * FROM workflow_scan_operations WHERE state='queued' ORDER BY created_at,scan_id"))

    def refresh_roots(self):
        with self.lock:
            rows = self.conn.execute("SELECT r.* FROM workflow_roots r JOIN workflow_host_state h USING(host_id) WHERE r.active=1 AND r.tombstoned=0 AND h.circuit='closed' AND h.configuration_required=0").fetchall()
            return tuple(r["root_id"] for r in rows if not r["last_success_at"] or (self.clock() - _time(r["last_success_at"])).total_seconds() >= self.profile.refresh_seconds)

    def probe_roots(self):
        with self.lock:
            rows = self.conn.execute("SELECT r.root_id,h.host_id,h.next_probe_at FROM workflow_roots r JOIN workflow_host_state h USING(host_id) WHERE r.active=1 AND r.tombstoned=0 AND h.circuit='open' AND h.configuration_required=0 AND NOT EXISTS(SELECT 1 FROM workflow_scan_operations o WHERE o.host_id=h.host_id AND o.owner_state IN ('owned','unknown_owner')) ORDER BY r.root_id").fetchall()
            result, hosts = [], set()
            for row in rows:
                if row["host_id"] not in hosts and (not row["next_probe_at"] or self.clock() >= _time(row["next_probe_at"])):
                    result.append(row["root_id"])
                    hosts.add(row["host_id"])
            return tuple(result)

    def get_scan(self, scan_id):
        with self.lock:
            row = self._one("SELECT * FROM workflow_scan_operations WHERE scan_id=?", (scan_id,))
            if row["finish_json"]:
                return ScanFinish.model_validate_json(row["finish_json"])
            return ScanFinish(scan_id=row["scan_id"], attempt_id=row["attempt_id"],
                              status="failed" if row["state"] == "unknown" else row["state"], owner_state=row["owner_state"])

    def _released(self, request, row, proof):
        if not proof or not proof.released or proof.attempt_id != request.attempt_id or proof.owner_generation != request.owner_generation:
            return False
        if row["owner_json"]:
            owner = ProcessOwner.model_validate_json(row["owner_json"])
            if owner.identity_uncertain or owner.unresolved_member_pids:
                return False
            expected = {canonical_json(b) for b in (owner.launcher, owner.worker, *owner.members) if b is not None}
            observed = {canonical_json(p.birth) for p in proof.processes if p.signaled}
            return expected <= observed
        return proof.no_process_created

    def finish(self, request: ScanRequest, outcome: ScanOutcome):
        with self._tx():
            row = self._bound(request)
            if row["owner_state"] == "released":
                return ScanFinish.model_validate_json(row["finish_json"])
            root = self._one("SELECT * FROM workflow_roots WHERE root_id=?", (request.root_id,))
            released = self._released(request, row, outcome.exit_proof)
            snapshot = outcome.snapshot
            error = outcome.error_code
            published = False
            source_id = root["legacy_source_id"]
            added = refreshed = missing = 0
            valid = (bool(row["publication_allowed"]) and not root["tombstoned"]
                     and root["root_revision"] == request.root_revision and root["mapping_revision"] == request.mapping_revision)
            if snapshot:
                valid = valid and all(getattr(snapshot, key) == getattr(request, key) for key in
                                      ("scan_id", "attempt_id", "root_id", "host_id", "export_id", "root_revision", "mapping_revision", "owner_generation", "kind"))
            if not released:
                error = "unknown_owner"
            elif not valid:
                error = "stale_binding"
            elif outcome.status == "complete" and snapshot and snapshot.coverage == "complete":
                same_root = _same_identity(snapshot.root_identity_before, snapshot.root_identity_after)
                matches = request.expected_root_identity is None or _same_identity(request.expected_root_identity, snapshot.root_identity_after)
                if not same_root or not matches:
                    error = "identity_changed"
                    self.conn.execute("UPDATE workflow_roots SET root_revision=root_revision+1,health='identity_changed' WHERE root_id=?", (request.root_id,))
                elif len(snapshot.actors) > self.profile.max_actors or len(snapshot.films) > self.profile.max_films:
                    error = "partial"
                else:
                    now = snapshot.observed_until.isoformat()
                    if request.kind == "probe_root":
                        if source_id is None:
                            cursor = self.conn.execute("INSERT INTO sources(unc_path,label) VALUES(?,?)", (root["private_path"], root["label"]))
                            source_id = cursor.lastrowid
                        self.conn.execute("UPDATE workflow_roots SET legacy_source_id=?,active=1,identity_json=?,probe_observed_at=?,probe_health='fresh' WHERE root_id=?",
                                          (source_id, canonical_json(snapshot.root_identity_after), now, request.root_id))
                    else:
                        result = apply_snapshot(self.conn, request, snapshot)
                        added, refreshed, missing = result.added, result.refreshed, result.missing
                        self._apply_locations(root, snapshot)
                        self.conn.execute("UPDATE workflow_roots SET identity_json=?,last_success_at=?,observed_at=?,health='fresh',probe_observed_at=?,probe_health='fresh' WHERE root_id=?",
                                          (canonical_json(snapshot.root_identity_after), now, now, now, request.root_id))
                    published = True
                    self._bump()
                    self.conn.execute("UPDATE workflow_host_state SET failures=0,circuit='closed',next_probe_at=NULL,configuration_required=0 WHERE host_id=?", (request.host_id,))
            else:
                error = error or ("partial" if outcome.status == "partial" else "cancelled" if outcome.status == "cancelled" else "protocol_error")
            if error and valid:
                self._failure(request, error, released)
            state = "complete" if published else "partial" if error == "partial" else "cancelled" if not row["publication_allowed"] or outcome.status == "cancelled" else "failed"
            result = ScanFinish(scan_id=request.scan_id, attempt_id=request.attempt_id, status=state,
                                owner_state="released" if released else "unknown_owner", source_id=source_id,
                                added=added, refreshed=refreshed, missing=missing, error_code=error, published=published)
            self.conn.execute("UPDATE workflow_scan_operations SET state=?,owner_state=?,outcome_json=?,finish_json=?,completed_at=? WHERE scan_id=?",
                              (state if released else "unknown", result.owner_state, canonical_json(outcome), canonical_json(result),
                               self.clock().isoformat() if released else None, request.scan_id))
            return result

    def _failure(self, request, error, released):
        health = error if error in {"offline", "permission_denied", "identity_changed", "identity_unknown", "unknown_owner"} else "partial"
        self.conn.execute("UPDATE workflow_roots SET health=?,probe_health=? WHERE root_id=?", (health, health, request.root_id))
        self._bump()
        if not released:
            return
        if error in {"permission_denied", "identity_changed", "identity_unknown"}:
            self.conn.execute("UPDATE workflow_host_state SET configuration_required=1 WHERE host_id=?", (request.host_id,))
        elif error not in {"cancelled", "stale_binding"}:
            host = self._one("SELECT failures FROM workflow_host_state WHERE host_id=?", (request.host_id,))
            failures = host[0] + 1
            delay = 900 if failures >= 3 else (30, 120)[failures - 1]
            self.conn.execute("UPDATE workflow_host_state SET failures=?,circuit=?,next_probe_at=? WHERE host_id=?",
                              (failures, "open" if failures >= 3 else "closed", (self.clock() + timedelta(seconds=delay)).isoformat(), request.host_id))

    def _apply_locations(self, root, snapshot):
        now = snapshot.observed_until.isoformat()
        previous = self.conn.execute("SELECT * FROM workflow_locations WHERE root_id=? AND retired=0", (root["root_id"],)).fetchall()
        seen = set()
        for actor in snapshot.actors:
            key = actor.relative_actor_path.casefold()
            old = next((p for p in previous if p["path_key"] == key), None)
            if old is None and actor.identity:
                old = next((p for p in previous if _same_identity(_identity(p["identity_json"]), actor.identity)), None)
            # Legacy unverified linkage may be adopted once; replacement of a
            # proved native object always receives a different location ID.
            if old and old["identity_json"] and not _same_identity(_identity(old["identity_json"]), actor.identity):
                self.conn.execute("UPDATE workflow_locations SET retired=1,state='unknown',legacy_performer_id=NULL,revision=revision+1 WHERE location_id=?", (old["location_id"],))
                old = None
            performer = self._one("SELECT id FROM performers WHERE source_id=? AND folder_name=?", (root["legacy_source_id"], actor.relative_actor_path))
            lid = old["location_id"] if old else str(uuid4())
            rev = old["revision"] + 1 if old else 1
            seen.add(lid)
            observation = str(uuid4())
            identity_json = canonical_json(actor.identity) if actor.identity else None
            if old:
                self.conn.execute("UPDATE workflow_locations SET legacy_performer_id=?,relative_path=?,path_key=?,name=?,name_norm=?,identity_json=?,last_seen_at=?,state='present',revision=?,observation_id=? WHERE location_id=?",
                                  (performer[0], actor.relative_actor_path, key, actor.name, normalize(actor.name), identity_json, now, rev, observation, lid))
            else:
                self.conn.execute("INSERT INTO workflow_locations(location_id,root_id,legacy_performer_id,relative_path,path_key,name,name_norm,identity_json,first_seen_at,last_seen_at,state,revision,observation_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                  (lid, root["root_id"], performer[0], actor.relative_actor_path, key, actor.name, normalize(actor.name), identity_json, now, now, "present", rev, observation))
            for evidence in actor.evidence:
                self.conn.execute("INSERT INTO workflow_location_evidence VALUES(?,?,?,?,?,?,?)",
                                  (str(uuid5(NAMESPACE_URL, lid + ":" + str(rev) + ":" + evidence.evidence_id)), lid, rev,
                                   evidence.kind, normalize(evidence.value), canonical_json(evidence), evidence.observed_at.isoformat()))
            self.conn.execute("UPDATE workflow_film_observations SET current=0 WHERE location_id=?", (lid,))
            for film in snapshot.films:
                if film.relative_actor_path.casefold() == key:
                    self.conn.execute("INSERT INTO workflow_film_observations VALUES(?,?,?,?,?,?,?,1)",
                                      (str(uuid4()), root["root_id"], lid, rev, film.relative_film_path, canonical_json(film), now))
        for old in previous:
            if old["location_id"] not in seen:
                self.conn.execute("UPDATE workflow_locations SET state='not_observed' WHERE location_id=? AND retired=0", (old["location_id"],))

    def tombstone_source_tx(self, source_id):
        roots = self.conn.execute("SELECT root_id FROM workflow_roots WHERE legacy_source_id=?", (source_id,)).fetchall()
        for row in roots:
            self.conn.execute("UPDATE workflow_locations SET state='unknown',retired=1,legacy_performer_id=NULL,revision=revision+1 WHERE root_id=?", (row[0],))
        self.conn.execute("UPDATE workflow_roots SET active=0,tombstoned=1,legacy_source_id=NULL,root_revision=root_revision+1,health='tombstoned' WHERE legacy_source_id=?", (source_id,))
        if roots:
            self._bump()

    def purge_performers_tx(self, performer_ids):
        for pid in performer_ids:
            self.conn.execute("UPDATE workflow_locations SET legacy_performer_id=NULL,state='not_observed',revision=revision+1 WHERE legacy_performer_id=?", (pid,))
        if performer_ids:
            self._bump()

    def aliases_changed_tx(self, name_norm):
        self._bump()

    def _coverage(self, row, revision):
        health = row["health"]
        last = _time(row["last_success_at"])
        if row["tombstoned"]:
            health = "tombstoned"
        elif health == "fresh" and (last is None or (self.clock() - last).total_seconds() > self.profile.freshness_seconds):
            health = "stale"
        return RootCoverage(root_id=row["root_id"], host_id=row["host_id"], export_id=row["export_id"], host_label=row["host_label"],
                            root_label=row["label"], root_revision=row["root_revision"], health=health,
                            coverage="complete" if health == "fresh" and last is not None else "incomplete",
                            last_attempt_at=_time(row["last_attempt_at"]), last_success_at=last,
                            probe_observed_at=_time(row["probe_observed_at"]), probe_health=row["probe_health"],
                            observed_at=_time(row["observed_at"]), index_revision=revision)

    def source_health(self, source_ids):
        with self.lock:
            revision = self._revision()
            result = {}
            for sid in source_ids:
                row = self.conn.execute("SELECT r.*,h.host_label FROM workflow_roots r JOIN workflow_host_state h USING(host_id) WHERE legacy_source_id=?", (sid,)).fetchone()
                if row:
                    result[sid] = self._coverage(row, revision)
            return result

    def _config(self, config, root_ids):
        if config.library_id != self.library_id:
            raise WorkflowIndexError("library_mismatch", 403)
        if any(root not in config.allowed_root_ids for root in root_ids):
            raise WorkflowIndexError("root_forbidden", 403)
        roots = []
        for rid in sorted(root_ids):
            row = self.conn.execute("SELECT r.*,h.host_label FROM workflow_roots r JOIN workflow_host_state h USING(host_id) WHERE root_id=? AND (active=1 OR tombstoned=1)", (rid,)).fetchone()
            if row is None:
                raise WorkflowIndexError("unknown_root", 422)
            roots.append(row)
        return roots

    def overview(self, config: WorkflowIndexConfig):
        with self.lock:
            revision = self._revision()
            roots = self._config(config, config.allowed_root_ids)
            return IndexOverview(library_id=self.library_id, component_id=config.component_id, index_revision=revision,
                                 generated_at=self.clock(), roots=tuple(self._coverage(r, revision) for r in roots))

    def query(self, query: CandidateQuery, config: WorkflowIndexConfig):
        with self.lock:
            return self._query(query, config)

    def _query(self, query, config):
        revision = self._revision()
        roots = self._config(config, query.root_ids)
        if query.expected_index_revision is not None and query.expected_index_revision != revision:
            raise WorkflowIndexError("index_changed")
        raw = query.model_dump(mode="json", exclude={"request_id", "cursor", "limit", "expected_index_revision"})
        raw["root_ids"] = sorted(raw["root_ids"])
        raw["actors"] = sorted(raw["actors"], key=lambda a: a["actor_ref"])
        query_hash = digest(raw)
        offset = 0
        if query.cursor:
            try:
                cursor = json.loads(base64.urlsafe_b64decode(query.cursor.encode()))
                if cursor["revision"] != revision or cursor["query_hash"] != query_hash:
                    raise WorkflowIndexError("index_changed")
                offset = cursor["offset"]
                if type(offset) is not int or offset < 0:
                    raise ValueError()
            except WorkflowIndexError:
                raise
            except (ValueError, KeyError, TypeError):
                raise WorkflowIndexError("invalid_cursor", 422) from None
        coverage = tuple(self._coverage(r, revision) for r in roots)
        complete = all(c.coverage == "complete" for c in coverage)
        candidates = []
        actor_counts = {a.actor_ref: 0 for a in query.actors}
        ambiguous = set()
        for root, cov in zip(roots, coverage):
            rows = self.conn.execute("SELECT * FROM workflow_locations WHERE root_id=? AND retired=0 AND state!='not_observed' ORDER BY location_id", (root["root_id"],)).fetchall()
            for row in rows:
                evidence = tuple(MetadataEvidence.model_validate_json(e[0]) for e in self.conn.execute("SELECT body_json FROM workflow_location_evidence WHERE location_id=? AND revision=? ORDER BY evidence_id", (row["location_id"], row["revision"])))
                aliases = [r[0] for r in self.conn.execute("SELECT alias FROM aliases WHERE name_norm=? ORDER BY alias_norm", (row["name_norm"],))]
                matches, reasons, rank = [], set(), 9
                for actor in query.actors:
                    names = [normalize(actor.name), *(normalize(a) for a in actor.aliases)]
                    external = [e for e in evidence if e.kind == "explicit_external_id" and e.namespace and e.status == "observed"]
                    ids_match = any(e.namespace == ext.namespace and e.value == ext.value for e in external for ext in actor.external_ids)
                    ids_conflict = any(e.namespace == ext.namespace and e.value != ext.value for e in external for ext in actor.external_ids)
                    match = "exact_external_id" if ids_match else "exact_name" if row["name_norm"] in names else "group_alias" if any(normalize(a) in names for a in aliases) else "fuzzy_name" if any(bidirectional_match(row["name_norm"], n) or any(bidirectional_match(normalize(a), n) for a in aliases) for n in names) else None
                    if match:
                        matches.append(actor.actor_ref)
                        actor_counts[actor.actor_ref] += 1
                        reasons.add(match)
                        rank = min(rank, {"exact_external_id": 0, "exact_name": 1, "group_alias": 2, "fuzzy_name": 3}[match])
                        if ids_conflict:
                            reasons.add("external_id_conflict")
                            ambiguous.add(actor.actor_ref)
                if not matches:
                    continue
                conflicts = []
                for film_row in self.conn.execute("SELECT body_json FROM workflow_film_observations WHERE location_id=? AND current=1 ORDER BY relative_path", (row["location_id"],)):
                    film = FilmObservation.model_validate_json(film_row[0])
                    why = []
                    if query.film.code and film.code and normalize(query.film.code) == normalize(film.code):
                        why.append("possible_same_code")
                    if query.film.title and film.title and normalize(query.film.title) == normalize(film.title):
                        why.append("same_name")
                    if why:
                        why.append("version_hint_differs" if query.film.edition_hints and film.edition_hints and query.film.edition_hints != film.edition_hints else "version_unknown")
                        conflicts.append(FilmConflict(relative_film_path=film.relative_film_path, reasons=tuple(why), observation_digest=digest(film)))
                root_identity = _identity(root["identity_json"])
                actor_identity = _identity(row["identity_json"])
                identity_state = "changed" if cov.health == "identity_changed" else "verified" if root_identity and actor_identity else "unknown"
                public = dict(candidate_id=digest({"library_id": self.library_id, "index_revision": revision, "query_hash": query_hash, "location_id": row["location_id"]}),
                              location_id=row["location_id"], location_revision=row["revision"], root_id=root["root_id"], root_revision=root["root_revision"],
                              host_id=root["host_id"], export_id=root["export_id"], host_label=root["host_label"], root_label=root["label"],
                              actor_display_name=row["name"], relative_actor_path=row["relative_path"], proposed_target_relative_path=row["relative_path"] + "/" + query.proposed_film_relative_path,
                              matched_actor_refs=tuple(sorted(matches)), match_reasons=tuple(sorted(reasons)),
                              external_id_evidence=tuple(e for e in evidence if e.kind == "explicit_external_id"), film_conflicts=tuple(conflicts),
                              observation_id=row["observation_id"], observed_at=_time(row["last_seen_at"]), last_success_at=cov.last_success_at,
                              identity_state=identity_state, native_identity=root_identity.identity if root_identity else None,
                              native_aliases=root_identity.aliases if root_identity else (), coverage_state=cov.coverage)
                # Validate/normalize typed nested values before canonical hashing.
                temporary = LocationCandidate(**public, candidate_digest="0" * 64)
                candidate = temporary.model_copy(update={"candidate_digest": digest({"query_hash": query_hash, "index_revision": revision,
                    "candidate": temporary.model_dump(mode="json", exclude={"candidate_digest"})})})
                candidates.append((rank, candidate))
        candidates.sort(key=lambda p: (p[0], p[1].location_id))
        actor_results = tuple(ActorMatch(actor_ref=a.actor_ref, total=actor_counts[a.actor_ref],
            match_state="ambiguous" if not complete or actor_counts[a.actor_ref] > 1 or a.actor_ref in ambiguous else "matched" if actor_counts[a.actor_ref] else "no_match",
            coverage_state="complete" if complete else "incomplete", reasons=() if complete else ("coverage_gap",)) for a in query.actors)
        state = "ambiguous" if any(a.match_state == "ambiguous" for a in actor_results) else "matched" if all(a.match_state == "matched" for a in actor_results) else "no_match" if all(a.match_state == "no_match" for a in actor_results) else "ambiguous"
        next_cursor = None
        if offset + query.limit < len(candidates):
            next_cursor = base64.urlsafe_b64encode(canonical_json({"revision": revision, "query_hash": query_hash, "offset": offset + query.limit}).encode()).decode()
        return CandidatePage(library_id=self.library_id, component_id=config.component_id, index_revision=revision,
            query_hash=query_hash, generated_at=self.clock(), roots=coverage, actor_results=actor_results,
            candidates=tuple(c for _, c in candidates[offset:offset + query.limit]), total=len(candidates),
            coverage_state="complete" if complete else "incomplete", match_state=state,
            reasons=() if complete else ("coverage_gap",), next_cursor=next_cursor)

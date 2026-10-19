"""Versioned, bounded F01 data. Safe to import in the scanner: no DB or app imports.

Private scan DTOs deliberately include the allowlisted local mapping. They must
never be returned by HTTP; public candidate DTOs have no such fields.
"""

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


class WorkflowIndexError(Exception):
    def __init__(self, code: str, status: int = 409):
        self.code, self.status = code, status
        super().__init__(code)


def _uuid(value: str) -> str:
    if str(UUID(value)) != value:
        raise ValueError("noncanonical UUID")
    return value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone required")
    return value.astimezone(timezone.utc)


def validate_relative_path(value: str) -> str:
    if not value or len(value) > 1024 or "\\" in value or any(ord(c) < 32 for c in value):
        raise ValueError("invalid relative path")
    for part in value.split("/"):
        if (not part or part in {".", ".."} or part.endswith((".", " "))
                or any(c in part for c in '<>:"|?*')
                or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])", part.split(".")[0])):
            raise ValueError("invalid relative path")
    return value


ID = Annotated[str, AfterValidator(_uuid)]
Revision = Annotated[int, Field(strict=True, ge=1)]
Count = Annotated[int, Field(strict=True, ge=0)]
Text = Annotated[str, Field(min_length=1, max_length=2048)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
RelativePath = Annotated[str, AfterValidator(validate_relative_path)]
UTC = Annotated[datetime, AfterValidator(_utc)]
ScanKind = Literal["probe_root", "scan_index"]
ErrorCode = Literal[
    "offline", "permission_denied", "not_directory", "identity_changed", "identity_unknown",
    "partial", "timeout", "cancelled", "protocol_error", "output_limit", "metadata_unknown",
    "thumbnail_unreadable", "unknown_owner", "native_failure", "stale_binding", "storage_error",
    "spawn_failed", "sampling_failed", "wait_failed", "query_failed", "close_failed", "io_unjoined",
]


class Model(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True,
                              ser_json_bytes="base64", val_json_bytes="base64")
    schema_version: Literal[1] = 1

    @field_validator("schema_version", mode="before")
    @classmethod
    def _version(cls, value):
        if type(value) is not int:
            raise ValueError("invalid schema version")
        return value


def canonical_json(value) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class ScanProfile(Model):
    max_owners: Annotated[int, Field(ge=1, le=4)] = 4
    queue_limit: Annotated[int, Field(ge=1, le=64)] = 64
    probe_seconds: Annotated[float, Field(gt=0, le=10)] = 10.0
    scan_seconds: Annotated[float, Field(gt=0, le=60)] = 60.0
    cleanup_seconds: Annotated[float, Field(gt=0, le=5)] = 5.0
    message_bytes: Annotated[int, Field(ge=1024, le=262144)] = 262144
    total_bytes: Annotated[int, Field(ge=1024, le=67108864)] = 67108864
    stderr_bytes: Annotated[int, Field(ge=0, le=32768)] = 32768
    max_actors: Annotated[int, Field(ge=1, le=10000)] = 10000
    max_films: Annotated[int, Field(ge=1, le=50000)] = 50000
    nfo_bytes: Annotated[int, Field(ge=1, le=1048576)] = 1048576
    refresh_seconds: Annotated[float, Field(gt=0)] = 21600.0
    freshness_seconds: Annotated[float, Field(gt=0)] = 86400.0


class NativeIdentity(Model):
    namespace: Literal["native128", "legacy64", "posix"]
    volume: Annotated[str, Field(pattern=r"^(0|[1-9][0-9]*)$", max_length=20)]
    object_id: Annotated[str, Field(min_length=1, max_length=40)]

    @model_validator(mode="after")
    def valid_identity(self):
        if int(self.volume) > 2**64 - 1:
            raise ValueError("volume out of range")
        if self.namespace == "native128":
            if not re.fullmatch("[0-9a-f]{32}", self.object_id) or self.object_id in {"0" * 32, "f" * 32}:
                raise ValueError("invalid native identity")
        else:
            if not re.fullmatch("[1-9][0-9]*", self.object_id) or int(self.object_id) >= 2**64 - 1:
                raise ValueError("invalid identity")
            if self.namespace == "legacy64" and int(self.volume) > 2**32 - 1:
                raise ValueError("invalid legacy volume")
        return self


class IdentityObservation(Model):
    observation_id: ID
    observed_at: UTC
    identity: NativeIdentity
    aliases: tuple[NativeIdentity, ...] = ()


class CachedThumbnail(Model):
    relative_actor_path: RelativePath
    mtime: float | None = None
    side: Count | None = None


class ThumbnailObservation(Model):
    state: Literal["unchanged", "replace", "absent", "unreadable"]
    mtime: float | None = None
    side: Count | None = None
    jpeg: Annotated[bytes | None, Field(max_length=524288)] = Field(default=None, repr=False)
    error_code: ErrorCode | None = None

    @model_validator(mode="after")
    def content(self):
        if self.mtime is not None and not math.isfinite(self.mtime):
            raise ValueError("invalid mtime")
        if self.state == "replace":
            if not self.jpeg or self.mtime is None or not self.side:
                raise ValueError("replacement evidence missing")
        elif self.jpeg is not None:
            raise ValueError("unexpected image content")
        return self


class ExternalID(Model):
    namespace: Text
    value: Text
    provenance_digest: Digest


class MetadataEvidence(Model):
    evidence_id: ID
    kind: Literal["name", "group_alias", "explicit_external_id", "film_metadata"]
    value: Text
    namespace: Text | None = None
    relative_metadata_path: RelativePath | None = None
    content_digest: Digest
    parser_version: Text = "f01-1"
    observed_at: UTC
    status: Literal["observed", "metadata_unknown"] = "observed"


class ActorObservation(Model):
    relative_actor_path: RelativePath
    name: Text
    identity: IdentityObservation | None = None
    evidence: tuple[MetadataEvidence, ...] = ()
    thumbnail: ThumbnailObservation = ThumbnailObservation(state="unchanged")


class FilmObservation(Model):
    relative_actor_path: RelativePath
    relative_film_path: RelativePath
    identity: IdentityObservation | None = None
    code: Text | None = None
    title: Text | None = None
    edition_hints: tuple[Text, ...] = ()
    evidence: tuple[MetadataEvidence, ...] = ()


class ScanRequest(Model):
    scan_id: ID
    attempt_id: ID
    root_id: ID
    host_id: ID
    export_id: ID
    root_revision: Revision
    mapping_revision: Revision
    owner_generation: Revision
    kind: ScanKind
    private_root_path: Annotated[str, Field(min_length=1, max_length=32767)] = Field(repr=False)
    expected_root_identity: IdentityObservation | None = None
    cached_thumbnails: tuple[CachedThumbnail, ...] = ()
    profile: ScanProfile = ScanProfile()


class ScanSnapshot(Model):
    scan_id: ID
    attempt_id: ID
    root_id: ID
    host_id: ID
    export_id: ID
    root_revision: Revision
    mapping_revision: Revision
    owner_generation: Revision
    kind: ScanKind
    observed_from: UTC
    observed_until: UTC
    root_identity_before: IdentityObservation
    root_identity_after: IdentityObservation
    coverage: Literal["complete", "partial"]
    actors: Annotated[tuple[ActorObservation, ...], Field(max_length=10000)] = ()
    films: Annotated[tuple[FilmObservation, ...], Field(max_length=50000)] = ()
    optional_errors: tuple[ErrorCode, ...] = ()

    @model_validator(mode="after")
    def relationships(self):
        if self.observed_until < self.observed_from:
            raise ValueError("invalid observation interval")
        keys = [a.relative_actor_path.casefold() for a in self.actors]
        film_keys = [f.relative_film_path.casefold() for f in self.films]
        if len(set(keys)) != len(keys) or len(set(film_keys)) != len(film_keys):
            raise ValueError("duplicate path")
        if any("/" in a.relative_actor_path for a in self.actors):
            raise ValueError("actor depth exceeded")
        for film in self.films:
            if (film.relative_actor_path.casefold() not in keys
                    or film.relative_film_path.count("/") != 1
                    or film.relative_film_path.split("/")[0].casefold() != film.relative_actor_path.casefold()):
                raise ValueError("film outside actor")
        if self.kind == "probe_root" and (self.actors or self.films):
            raise ValueError("probe is not an index scan")
        return self


class ProcessBirth(Model):
    pid: Revision
    created_filetime_100ns: Revision
    role: Literal["launcher", "worker", "member"]


class RuntimeIdentity(Model):
    pid: Annotated[int, Field(strict=True, ge=1, le=0xFFFFFFFF)]
    created_filetime_100ns: Annotated[int, Field(strict=True, ge=1, lt=1 << 64)]
    session_id: Annotated[int, Field(strict=True, ge=0, le=0xFFFFFFFF)]
    machine_digest: Digest


class ProcessOwner(Model):
    runner_instance_id: ID
    job_name: Text = Field(repr=False)
    launcher: ProcessBirth
    worker: ProcessBirth | None = None
    members: tuple[ProcessBirth, ...] = ()
    observed_at: UTC
    identity_uncertain: bool = False
    unresolved_member_pids: tuple[Revision, ...] = ()
    runtime: RuntimeIdentity | None = None


class ProcessExit(Model):
    birth: ProcessBirth
    signaled: bool


class ExitProof(Model):
    attempt_id: ID
    owner_generation: Revision
    processes: tuple[ProcessExit, ...] = ()
    job_empty: bool = False
    io_joined: bool = False
    handles_closed: bool = False
    identity_uncertain: bool = False
    no_process_created: bool = False
    failure_codes: tuple[ErrorCode, ...] = ()
    checked_at: UTC

    @model_validator(mode="after")
    def creation_fact(self):
        if self.no_process_created and self.processes:
            raise ValueError("conflicting process creation evidence")
        return self

    @property
    def released(self) -> bool:
        return bool((self.no_process_created or self.processes) and all(p.signaled for p in self.processes)
                    and self.job_empty and self.io_joined and self.handles_closed
                    and not self.identity_uncertain and not self.failure_codes)


class RecoveredBirth(Model):
    pid: Annotated[int, Field(strict=True, ge=1, le=0xFFFFFFFF)]
    expected_created_filetime_100ns: Annotated[int, Field(strict=True, ge=1, lt=1 << 64)]
    role: Literal["runtime", "launcher", "worker", "member"]
    disposition: Literal["handle_signaled", "pid_absent", "birth_reused"]
    observed_created_filetime_100ns: Annotated[int, Field(strict=True, ge=1, lt=1 << 64)] | None = None

    @model_validator(mode="after")
    def coherent(self):
        observed = self.observed_created_filetime_100ns
        if ((self.disposition == "pid_absent" and observed is not None)
                or (self.disposition == "handle_signaled" and observed != self.expected_created_filetime_100ns)
                or (self.disposition == "birth_reused" and (observed is None or observed == self.expected_created_filetime_100ns))):
            raise ValueError("invalid recovered birth evidence")
        return self


class RecoveredOwnerProof(Model):
    scan_id: ID
    attempt_id: ID
    owner_generation: Revision
    request_digest: Digest
    owner_digest: Digest
    inspector_runtime: RuntimeIdentity
    original_runtime: RuntimeIdentity
    runtime: RecoveredBirth
    processes: tuple[RecoveredBirth, ...]
    job_name: Text = Field(repr=False)
    job_state: Literal["empty", "absent"]
    handles_closed: bool
    checked_at: UTC

    @model_validator(mode="after")
    def coherent(self):
        old, current, proof = self.original_runtime, self.inspector_runtime, self.runtime
        if ((old.machine_digest, old.session_id) != (current.machine_digest, current.session_id)
                or proof.role != "runtime" or (proof.pid, proof.expected_created_filetime_100ns)
                != (old.pid, old.created_filetime_100ns)
                or (old.pid, old.created_filetime_100ns) == (current.pid, current.created_filetime_100ns)
                or not self.processes or len(self.processes) > 34
                or any(p.role == "runtime" for p in self.processes)
                or len({(p.pid, p.expected_created_filetime_100ns, p.role) for p in self.processes}) != len(self.processes)):
            raise ValueError("invalid recovered owner evidence")
        return self

    @property
    def released(self) -> bool:
        return self.handles_closed


class ScanTicket(Model):
    scan_id: ID
    attempt_id: ID
    root_id: ID
    host_id: ID
    kind: ScanKind


class ScanOutcome(Model):
    status: Literal["complete", "failed", "partial", "cancelled"]
    owner_state: Literal["released", "unknown_owner"]
    snapshot: ScanSnapshot | None = None
    exit_proof: ExitProof | None = None
    recovery_proof: RecoveredOwnerProof | None = None
    error_code: ErrorCode | None = None


class ScanFinish(Model):
    scan_id: ID
    attempt_id: ID
    status: Literal["queued", "starting", "running", "complete", "failed", "partial", "cancelled"]
    owner_state: Literal["none", "owned", "released", "unknown_owner"]
    source_id: Revision | None = None
    added: Count = 0
    refreshed: Count = 0
    missing: Count = 0
    error_code: ErrorCode | None = None
    published: bool = False


class PendingOwner(Model):
    request: ScanRequest
    owner: ProcessOwner | None = None
    state: Literal["starting", "running", "unknown_owner"]


class ProbeDraft(Model):
    private_root_path: Annotated[str, Field(min_length=1, max_length=32767)] = Field(repr=False)
    label: Text
    host_id: ID | None = None
    export_id: ID | None = None
    host_label: Text = "NAS"


Health = Literal["fresh", "stale", "never_scanned", "offline", "permission_denied",
                 "identity_changed", "identity_unknown", "partial", "unknown_owner", "tombstoned"]


class RootCoverage(Model):
    root_id: ID
    host_id: ID
    export_id: ID
    host_label: Text
    root_label: Text
    root_revision: Revision
    health: Health
    coverage: Literal["complete", "incomplete"]
    last_attempt_at: UTC | None = None
    last_success_at: UTC | None = None
    probe_observed_at: UTC | None = None
    probe_health: Health = "never_scanned"
    observed_at: UTC | None = None
    index_revision: Revision


class ActorQuery(Model):
    actor_ref: ID
    name: Text
    aliases: Annotated[tuple[Text, ...], Field(max_length=32)] = ()
    external_ids: Annotated[tuple[ExternalID, ...], Field(max_length=16)] = ()


class FilmQuery(Model):
    code: Text | None = None
    title: Text | None = None
    edition_hints: tuple[Text, ...] = ()


class CandidateQuery(Model):
    request_id: ID
    film_id: ID
    metadata_revision: Revision
    final_revision: Revision
    final_hash: Digest
    actors: Annotated[tuple[ActorQuery, ...], Field(min_length=1, max_length=32)]
    film: FilmQuery = FilmQuery()
    root_ids: Annotated[tuple[ID, ...], Field(min_length=1, max_length=256)]
    proposed_film_relative_path: RelativePath
    expected_index_revision: Revision | None = None
    cursor: Annotated[str | None, Field(max_length=1024)] = None
    limit: Annotated[int, Field(ge=1, le=200)] = 50

    @model_validator(mode="after")
    def unique(self):
        if len({a.actor_ref for a in self.actors}) != len(self.actors) or len(set(self.root_ids)) != len(self.root_ids):
            raise ValueError("duplicate query member")
        if any(not a.name.strip() or any(not s.strip() for s in a.aliases) for a in self.actors):
            raise ValueError("empty name")
        return self


class ActorMatch(Model):
    actor_ref: ID
    match_state: Literal["matched", "ambiguous", "no_match"]
    coverage_state: Literal["complete", "incomplete"]
    total: Count
    reasons: tuple[Text, ...] = ()


class FilmConflict(Model):
    relative_film_path: RelativePath
    reasons: tuple[Literal["possible_same_code", "same_name", "version_hint_differs", "version_unknown"], ...]
    observation_digest: Digest


class LocationCandidate(Model):
    candidate_id: Digest
    location_id: ID
    location_revision: Revision
    root_id: ID
    root_revision: Revision
    host_id: ID
    export_id: ID
    host_label: Text
    root_label: Text
    actor_display_name: Text
    relative_actor_path: RelativePath
    proposed_target_relative_path: RelativePath
    matched_actor_refs: tuple[ID, ...]
    match_reasons: tuple[Text, ...]
    external_id_evidence: tuple[MetadataEvidence, ...]
    film_conflicts: tuple[FilmConflict, ...]
    observation_id: ID
    observed_at: UTC
    last_success_at: UTC | None
    identity_state: Literal["verified", "unknown", "changed"]
    native_identity: NativeIdentity | None
    native_aliases: tuple[NativeIdentity, ...] = ()
    coverage_state: Literal["complete", "incomplete"]
    candidate_digest: Digest


class CandidatePage(Model):
    library_id: ID
    component_id: ID
    index_revision: Revision
    query_hash: Digest
    generated_at: UTC
    roots: tuple[RootCoverage, ...]
    actor_results: tuple[ActorMatch, ...]
    candidates: tuple[LocationCandidate, ...]
    total: Count
    coverage_state: Literal["complete", "incomplete"]
    match_state: Literal["matched", "ambiguous", "no_match"]
    reasons: tuple[Text, ...] = ()
    next_cursor: str | None = None


class IndexOverview(Model):
    library_id: ID
    component_id: ID
    index_revision: Revision
    generated_at: UTC
    roots: tuple[RootCoverage, ...]


class WorkflowIndexConfig(Model):
    component_id: ID
    library_id: ID
    read_token: SecretStr = Field(repr=False)
    allowed_root_ids: tuple[ID, ...]

    @model_validator(mode="after")
    def token_not_empty(self):
        if not self.read_token.get_secret_value().strip():
            raise ValueError("empty credential")
        if len(set(self.allowed_root_ids)) != len(self.allowed_root_ids):
            raise ValueError("duplicate allowed root")
        return self

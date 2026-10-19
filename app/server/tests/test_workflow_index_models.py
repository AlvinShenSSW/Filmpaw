from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from filmpaw_server.workflow_models import (
    CandidateQuery, ExitProof, NativeIdentity, ProcessBirth, ProcessExit,
    ScanProfile, ThumbnailObservation, canonical_json, digest, validate_relative_path,
)


def test_strict_version_and_integer_bounds():
    for kwargs in ({"schema_version": True}, {"schema_version": 2}, {"max_owners": True},
                   {"max_owners": 0}, {"unplanned": 1}):
        with pytest.raises(ValidationError):
            ScanProfile(**kwargs)


@pytest.mark.parametrize("value", ["/root", "C:/root", "//nas/root", "a/../b", "a//b",
                                  "a\\b", "a:stream", "NUL", "a/COM1.txt", "a.", "a "])
def test_relative_path_rejects_windows_escapes(value):
    with pytest.raises(ValueError):
        validate_relative_path(value)


def test_digest_is_key_order_stable_but_binding_sensitive():
    assert canonical_json({"b": 2, "a": 1}) == '{"a":1,"b":2}'
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})
    assert digest({"revision": 1}) != digest({"revision": 2})


def test_native_namespaces_do_not_truncate_or_accept_invalid_identity():
    native = NativeIdentity(namespace="native128", volume="123", object_id="01" * 16)
    legacy = NativeIdentity(namespace="legacy64", volume="123", object_id="1")
    assert digest(native) != digest(legacy)
    for obj in ("00" * 16, "ff" * 16, "1"):
        with pytest.raises(ValidationError):
            NativeIdentity(namespace="native128", volume="123", object_id=obj)


def test_exit_proof_requires_all_evidence_not_boolean_claim():
    birth = ProcessBirth(pid=42, created_filetime_100ns=100, role="worker")
    proof = ExitProof(attempt_id=str(uuid4()), owner_generation=1,
                      processes=(ProcessExit(birth=birth, signaled=True),),
                      job_empty=True, io_joined=True, handles_closed=True,
                      checked_at=datetime.now(timezone.utc))
    assert proof.released
    assert not proof.model_copy(update={"identity_uncertain": True}).released
    assert not proof.model_copy(update={"processes": ()}).released
    assert not proof.model_copy(update={"handles_closed": False}).released


def test_definite_no_creation_is_distinct_from_unknown_spawn():
    binding = dict(attempt_id=str(uuid4()), owner_generation=1,
                   job_empty=True, io_joined=True, handles_closed=True,
                   checked_at=datetime.now(timezone.utc))
    assert not ExitProof(**binding).released
    assert ExitProof(**binding, no_process_created=True).released
    with pytest.raises(ValidationError):
        ExitProof(**binding, no_process_created=True,
                  processes=(ProcessExit(birth=ProcessBirth(pid=1, created_filetime_100ns=1, role="launcher"), signaled=True),))


def test_thumbnail_discriminated_content():
    with pytest.raises(ValidationError):
        ThumbnailObservation(state="replace")
    with pytest.raises(ValidationError):
        ThumbnailObservation(state="absent", jpeg=b"data")


def test_query_rejects_duplicate_actor_and_root_members():
    actor = {"actor_ref": str(uuid4()), "name": "Example"}
    body = dict(request_id=str(uuid4()), film_id=str(uuid4()), metadata_revision=1,
                final_revision=1, final_hash="a" * 64, actors=(actor, actor),
                root_ids=(str(uuid4()),), proposed_film_relative_path="Film")
    with pytest.raises(ValidationError):
        CandidateQuery(**body)

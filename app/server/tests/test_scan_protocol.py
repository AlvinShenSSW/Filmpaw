"""Bounded private scanner protocol; no network or application lifetime."""

import io

import pytest

from filmpaw_server.scan_protocol import (
    FRAME_BYTES, PayloadReader, ProtocolError, encode_frame, payload_frames, read_frame,
)

BINDING = {"scan_id": "scan", "attempt_id": "attempt", "owner_generation": 1}


def test_large_unicode_snapshot_crosses_frames_without_changing_hash():
    original = {"actors": [{"name": "合成演员", "data": "字幕😀" * 60000}]}
    frames = list(payload_frames(original, BINDING))
    assert len(frames) > 2
    receiver = PayloadReader(BINDING)
    result = None
    for frame in frames:
        encoded = encode_frame(frame)
        assert len(encoded) <= FRAME_BYTES
        result = receiver.accept(read_frame(io.BytesIO(encoded)))
    assert result == original


@pytest.mark.parametrize("fault", ["sequence", "binding", "digest", "duplicate", "after_complete"])
def test_corrupt_or_replayed_frames_never_form_complete_snapshot(fault):
    frames = list(payload_frames({"ok": True}, BINDING))
    receiver = PayloadReader(BINDING)
    if fault == "sequence":
        frames[0]["sequence"] = 1
    elif fault == "binding":
        frames[0]["attempt_id"] = "another"
    elif fault == "digest":
        frames[-1]["sha256"] = "0" * 64
    elif fault == "duplicate":
        frames.insert(1, frames[0])
    else:
        frames.append(frames[-1])
    with pytest.raises(ProtocolError):
        for frame in frames:
            receiver.accept(frame)


@pytest.mark.parametrize("raw", [
    b'{"x":1,"x":2}\n', b'{"x":NaN}\n', b'{}', b'\xff\n', b'[]\n',
    b' ' * FRAME_BYTES + b'\n',
], ids=["duplicate", "nonfinite", "short_eof", "utf8", "nonobject", "oversize"])
def test_invalid_or_unbounded_frame_rejected(raw):
    with pytest.raises(ProtocolError):
        read_frame(io.BytesIO(raw))


def test_partial_payload_and_total_bound_are_not_success():
    frames = list(payload_frames({"data": "x" * 300}, BINDING))
    receiver = PayloadReader(BINDING)
    assert receiver.accept(frames[0]) is None
    assert not receiver.complete
    limited = PayloadReader(BINDING, total_limit=100)
    with pytest.raises(ProtocolError, match="payload_oversize"):
        limited.accept(frames[0])

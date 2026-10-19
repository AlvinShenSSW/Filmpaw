"""Bounded JSON frames for the fixed scanner's private inherited pipes."""

import base64
import binascii
import hashlib
import json

FRAME_BYTES = 256 * 1024
RESULT_BYTES = 64 * 1024 * 1024
CHUNK_BYTES = 128 * 1024


class ProtocolError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ProtocolError("invalid_json") from None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("duplicate_json_key")
        result[key] = value
    return result


def _constant(_value):
    raise ProtocolError("invalid_json")


def decode(raw):
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ProtocolError("invalid_json") from None
    if not isinstance(value, dict):
        raise ProtocolError("invalid_json")
    return value


def encode_frame(value):
    raw = canonical(value) + b"\n"
    if len(raw) > FRAME_BYTES:
        raise ProtocolError("frame_oversize")
    return raw


def read_frame(stream):
    raw = stream.readline(FRAME_BYTES + 1)
    if not raw.endswith(b"\n") or len(raw) > FRAME_BYTES:
        raise ProtocolError("frame_incomplete")
    return decode(raw)


def payload_frames(value, binding, *, kind="result", total_limit=RESULT_BYTES):
    raw = canonical(value)
    if len(raw) > total_limit:
        raise ProtocolError("payload_oversize")
    count = 0
    for offset in range(0, len(raw), CHUNK_BYTES):
        yield {**binding, "type": kind + "_chunk", "sequence": count,
               "data": base64.b64encode(raw[offset:offset + CHUNK_BYTES]).decode("ascii")}
        count += 1
    yield {**binding, "type": kind + "_complete", "chunks": count,
           "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


class PayloadReader:
    def __init__(self, binding, *, kind="result", total_limit=RESULT_BYTES):
        self.binding, self.kind, self.limit = binding, kind, total_limit
        self.buffer = bytearray()
        self.sequence = 0
        self.complete = False

    def accept(self, frame):
        if self.complete or any(frame.get(key) != value or type(frame.get(key)) is not type(value)
                                for key, value in self.binding.items()):
            raise ProtocolError("frame_binding")
        common = set(self.binding) | {"type"}
        if frame.get("type") == self.kind + "_chunk":
            if (set(frame) != common | {"sequence", "data"}
                    or type(frame["sequence"]) is not int or frame["sequence"] != self.sequence
                    or not isinstance(frame["data"], str)):
                raise ProtocolError("frame_sequence")
            try:
                data = base64.b64decode(frame["data"], validate=True)
            except (ValueError, binascii.Error):
                raise ProtocolError("frame_encoding") from None
            if len(data) > CHUNK_BYTES or not data:
                raise ProtocolError("frame_oversize")
            if len(self.buffer) + len(data) > self.limit:
                raise ProtocolError("payload_oversize")
            self.buffer.extend(data)
            self.sequence += 1
            return None
        if (frame.get("type") != self.kind + "_complete"
                or set(frame) != common | {"chunks", "bytes", "sha256"}
                or type(frame["chunks"]) is not int or frame["chunks"] != self.sequence
                or type(frame["bytes"]) is not int or frame["bytes"] != len(self.buffer)
                or frame["sha256"] != hashlib.sha256(self.buffer).hexdigest()):
            raise ProtocolError("payload_incomplete")
        value = decode(self.buffer)
        self.complete = True
        self.buffer.clear()
        return value

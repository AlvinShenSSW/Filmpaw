"""Fixed, read-only scanner. The private GO protocol is required before any I/O."""

import hashlib
import io
import os
import stat
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from PIL import Image

from filmpaw_server.directory_reads import directory_error_retryable
from filmpaw_server.normalize import normalize
from filmpaw_server.path_resources import capture
from filmpaw_server.scan_protocol import PayloadReader, ProtocolError, canonical, encode_frame, payload_frames, read_frame
from filmpaw_server.workflow_models import (
    ActorObservation, FilmObservation, IdentityObservation, MetadataEvidence,
    NativeIdentity, ScanRequest, ScanSnapshot, ThumbnailObservation, validate_relative_path,
)

BINDING_FIELDS = ("scan_id", "attempt_id", "root_id", "host_id", "export_id",
                  "root_revision", "mapping_revision", "owner_generation", "kind")


class CollectionError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def now():
    return datetime.now(timezone.utc)


def binding(request):
    return {name: getattr(request, name) for name in BINDING_FIELDS}


def _native(identity):
    if identity[0] == "legacy64":
        return NativeIdentity(namespace="legacy64", volume=str(identity[1]), object_id=str(identity[2]))
    return NativeIdentity(namespace="native128" if sys.platform == "win32" else "posix",
                          volume=str(identity[0]), object_id=str(identity[1]))


def _observation(resource):
    return IdentityObservation(observation_id=str(uuid4()), observed_at=now(),
                               identity=_native(resource["identity"]),
                               aliases=tuple(_native(i) for i in resource.get("identity_aliases", ())))


def _same(first, second):
    return first["identity"] == second["identity"] and first["ancestors"] == second["ancestors"]


def _code(error):
    if directory_error_retryable(error):
        return "offline"
    if isinstance(error, PermissionError) or getattr(error, "winerror", None) == 5:
        return "permission_denied"
    if isinstance(error, (FileNotFoundError, NotADirectoryError)):
        return "not_directory"
    return "identity_unknown"


def _directories(path, limit):
    result = []
    with os.scandir(path) as entries:
        for entry in entries:
            if entry.name.startswith("."):
                continue
            info = entry.stat(follow_symlinks=False)
            if getattr(info, "st_file_attributes", 0) & 6:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise CollectionError("identity_unknown")
            if not stat.S_ISDIR(info.st_mode):
                continue
            validate_relative_path(entry.name)
            result.append(entry.name)
            if len(result) > limit:
                raise CollectionError("partial")
    if len({name.casefold() for name in result}) != len(result):
        raise CollectionError("partial")
    return sorted(result, key=str.casefold)


def _read_file(path, limit):
    """Read a bounded plain file, refusing a Windows reparse point on its handle."""
    if sys.platform == "win32":
        import ctypes
        import msvcrt
        from ctypes import wintypes as w
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE]
        kernel.CreateFileW.restype = w.HANDLE
        kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [w.HANDLE], w.BOOL
        kernel.GetFileInformationByHandleEx.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD]
        kernel.GetFileInformationByHandleEx.restype = w.BOOL
        handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            attributes = (w.DWORD * 2)()
            if not kernel.GetFileInformationByHandleEx(handle, 9, attributes, ctypes.sizeof(attributes)):
                raise ctypes.WinError(ctypes.get_last_error())
            if attributes[0] & (0x400 | 0x10):
                raise CollectionError("identity_unknown")
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
            handle = None
        finally:
            if handle is not None and not kernel.CloseHandle(handle):
                raise OSError("read_handle_close")
    else:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise CollectionError("partial")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise CollectionError("partial")
        return data, info.st_mtime


def _thumbnail(folder, cached, limit):
    path = folder / "folder.jpg"
    try:
        data, mtime = _read_file(path, limit)
        if cached is not None and cached.mtime == mtime and cached.side == 512:
            return ThumbnailObservation(state="unchanged")
        with Image.open(io.BytesIO(data)) as image:
            image.draft("RGB", (512, 512))
            image = image.convert("RGB")
            image.thumbnail((512, 512))
            result = io.BytesIO()
            image.save(result, format="JPEG", quality=80)
        return ThumbnailObservation(state="replace", mtime=mtime, side=512, jpeg=result.getvalue())
    except FileNotFoundError as error:
        if not directory_error_retryable(error):
            return ThumbnailObservation(state="absent")
    except (OSError, ValueError, CollectionError, Image.DecompressionBombError):
        pass
    return ThumbnailObservation(state="unreadable", error_code="thumbnail_unreadable")


def _metadata(folder, relative, actor_name, limit):
    evidence, actor_evidence, errors = [], [], []
    code = title = None
    with os.scandir(folder) as entries:
        names = []
        for entry in entries:
            if entry.name.lower().endswith(".nfo"):
                validate_relative_path(entry.name)
                names.append(entry.name)
                if len(names) > 128:
                    return None, None, (), (), ("metadata_unknown",)
    for name in sorted(names):
        item = relative + "/" + name
        try:
            data, _ = _read_file(folder / name, limit)
            # The supported layouts use UTF-8. Wide encodings must not bypass
            # the declaration guard and activate the XML entity processor.
            data.decode("utf-8-sig")
            if b"\x00" in data or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
                raise ValueError("unsupported XML")
            root = ET.fromstring(data)
            if root.tag not in {"movie", "actor"}:
                raise ValueError("unsupported metadata")
            content_hash = hashlib.sha256(data).hexdigest()
            observed = now()
            if root.tag == "movie":
                code = code or root.findtext("num") or root.findtext("code")
                title = title or root.findtext("title")
                for value in (code, title):
                    if value and len(value) <= 2048:
                        evidence.append(MetadataEvidence(evidence_id=str(uuid4()), kind="film_metadata",
                            value=value, relative_metadata_path=item, content_digest=content_hash, observed_at=observed))
            actors = [root] if root.tag == "actor" else list(root.findall("actor"))
            for actor in actors:
                name_value = actor.findtext("name")
                if not name_value or normalize(name_value) != normalize(actor_name):
                    continue  # Co-stars are not identities of this actor directory.
                for identifier in actor.findall("uniqueid"):
                    namespace, value = identifier.get("type"), identifier.text
                    if namespace and value and len(namespace) <= 2048 and len(value) <= 2048:
                        actor_evidence.append(MetadataEvidence(evidence_id=str(uuid4()), kind="explicit_external_id",
                            namespace=namespace, value=value, relative_metadata_path=item,
                            content_digest=content_hash, observed_at=observed))
        except (OSError, ValueError, ET.ParseError, CollectionError):
            errors.append("metadata_unknown")
    if code and len(code) > 2048:
        code = None
        errors.append("metadata_unknown")
    if title and len(title) > 2048:
        title = None
        errors.append("metadata_unknown")
    return code, title, tuple(evidence), tuple(actor_evidence), tuple(errors)


def collect_snapshot(request):
    start = now()
    root = Path(request.private_root_path)
    try:
        before = capture(root)
        before_observation = _observation(before)
        if (request.expected_root_identity is not None
                and request.expected_root_identity.identity != before_observation.identity):
            raise CollectionError("identity_changed")
        actors, films, errors = [], [], []
        cached = {item.relative_actor_path.casefold(): item for item in request.cached_thumbnails}
        if request.kind == "scan_index":
            for name in _directories(root, request.profile.max_actors):
                actor_path = root / name
                actor_before = capture(actor_path)
                _, _, _, actor_evidence, issues = _metadata(actor_path, name, name, request.profile.nfo_bytes)
                errors.extend(issues)
                for movie in _directories(actor_path, request.profile.max_films - len(films)):
                    relative = name + "/" + movie
                    movie_path = actor_path / movie
                    movie_before = capture(movie_path)
                    code, title, metadata, ids, issues = _metadata(movie_path, relative, name, request.profile.nfo_bytes)
                    errors.extend(issues)
                    actor_evidence += ids
                    if not _same(movie_before, capture(movie_path)):
                        raise CollectionError("identity_changed")
                    films.append(FilmObservation(relative_actor_path=name, relative_film_path=relative,
                                  identity=_observation(movie_before), code=code, title=title, evidence=metadata))
                thumbnail = _thumbnail(actor_path, cached.get(name.casefold()), request.profile.total_bytes)
                if thumbnail.error_code:
                    errors.append(thumbnail.error_code)
                if not _same(actor_before, capture(actor_path)):
                    raise CollectionError("identity_changed")
                actors.append(ActorObservation(relative_actor_path=name, name=name,
                              identity=_observation(actor_before), evidence=actor_evidence, thumbnail=thumbnail))
        after = capture(root)
        if not _same(before, after):
            raise CollectionError("identity_changed")
        return ScanSnapshot(**binding(request), observed_from=start, observed_until=now(),
                            root_identity_before=before_observation, root_identity_after=_observation(after),
                            coverage="complete", actors=tuple(actors), films=tuple(films),
                            optional_errors=tuple(sorted(set(errors))))
    except OSError as error:
        raise CollectionError(_code(error)) from None
    except ValueError:
        raise CollectionError("partial") from None


def worker_main(stdin=None, stdout=None):
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    initial = None
    try:
        initial = read_frame(stdin)
        if (set(initial) != {"type", "scan_id", "attempt_id", "owner_generation"}
                or initial["type"] != "init"):
            raise ProtocolError("invalid_init")
        key = {k: v for k, v in initial.items() if k != "type"}
        stdout.write(encode_frame({**key, "type": "hello", "pid": os.getpid(), "schema_version": 1}))
        stdout.flush()
        go = read_frame(stdin)
        if go != {**key, "type": "go"}:
            raise ProtocolError("missing_go")
        receiver = PayloadReader(key, kind="request")
        value = None
        while value is None:
            value = receiver.accept(read_frame(stdin))
        request = ScanRequest.model_validate_json(canonical(value))
        if any(getattr(request, k) != v for k, v in key.items()):
            raise ProtocolError("request_binding")
        result = collect_snapshot(request)
        for frame in payload_frames(result.model_dump(mode="json"), key,
                                    total_limit=request.profile.total_bytes):
            stdout.write(encode_frame(frame))
        stdout.flush()
        return 0
    except CollectionError as error:
        if initial is not None:
            stdout.write(encode_frame({**key, "type": "error", "code": error.code}))
            stdout.flush()
        return 1
    except (ProtocolError, OSError, ValueError):
        return 2

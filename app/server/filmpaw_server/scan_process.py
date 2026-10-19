"""Fixed scanner supervisor. The repository owns SQL; this module owns processes."""

import asyncio
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from filmpaw_server.scan_native import NativeOwner, WindowsAPI
from filmpaw_server.scan_recovery import RecoveryInspection
from filmpaw_server.scan_protocol import PayloadReader, ProtocolError, canonical, decode, encode_frame, payload_frames
from filmpaw_server.workflow_models import (
    ExitProof, ProcessBirth, ProcessExit, ProcessOwner, RuntimeIdentity, ScanOutcome, ScanSnapshot,
    ScanTicket, WorkflowIndexError,
)


def _now():
    return datetime.now(timezone.utc)


def _launch():
    """Only this installed worker; never accept commands from the caller."""
    frozen = bool(getattr(sys, "frozen", False))
    argv = [sys.executable, "--workflow-nas-worker"] if frozen else [
        sys.executable, "-m", "filmpaw_server.main", "--workflow-nas-worker"]
    allowed = {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "PATH", "TEMP", "TMP", "COMSPEC", "LANG"}
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed or k.startswith("_PYI_")}
    cwd = str(Path(sys.executable).parent) if frozen else str(Path(__file__).resolve().parent.parent)
    return argv, env, cwd


def _birth(member):
    return ProcessBirth(pid=member.pid, created_filetime_100ns=member.created, role=member.role)


class _Attempt:
    def __init__(self, index, request, runner_id):
        self.index, self.request, self.runner_id = index, request, runner_id
        self.native = NativeOwner()
        self.runtime = None
        self.cancel = threading.Event()
        self.stopping = threading.Event()
        self.go = threading.Event()
        self.failure = threading.Event()
        self.frames = queue.Queue(maxsize=8)
        self.io = []
        self.snapshot = None
        self.error = None
        self.result = None
        self.status = "failed"
        self.lock = threading.Lock()
        self.key = {k: getattr(request, k) for k in ("scan_id", "attempt_id", "owner_generation")}
        self.thread = threading.Thread(target=self.run, name="filmpaw-scan", daemon=True)

    def _put(self, value):
        while not self.stopping.is_set():
            try:
                self.frames.put(value, timeout=.05)
                return
            except queue.Full:
                pass

    def _stdout(self):
        buffered = bytearray()
        try:
            while chunk := self.native.read_stdout():
                buffered.extend(chunk)
                while b"\n" in buffered:
                    end = buffered.index(b"\n")
                    if end + 1 > self.request.profile.message_bytes:
                        raise ProtocolError("frame_limit")
                    self._put(decode(bytes(buffered[:end])))
                    del buffered[:end + 1]
                if len(buffered) >= self.request.profile.message_bytes:
                    raise ProtocolError("frame_limit")
            if buffered:
                raise ProtocolError("short_eof")
            self._put(None)
        except (OSError, ProtocolError):
            self.failure.set()

    def _stderr(self):
        # Raw child output can contain a private path. Count and discard it.
        count = 0
        try:
            while chunk := self.native.read_stderr():
                count += len(chunk)
                if count > self.request.profile.stderr_bytes:
                    self.failure.set()
        except OSError:
            self.failure.set()

    def _writer(self):
        try:
            self.native.write(encode_frame({**self.key, "type": "init"}))
            while not self.go.wait(.05):
                if self.stopping.is_set() or self.cancel.is_set():
                    return
            if self.stopping.is_set() or self.cancel.is_set():
                return
            self.native.write(encode_frame({**self.key, "type": "go"}))
            for frame in payload_frames(self.request.model_dump(mode="json"), self.key, kind="request"):
                if self.stopping.is_set():
                    return
                self.native.write(encode_frame(frame))
        except (OSError, ProtocolError):
            self.failure.set()

    def _observation(self):
        members = self.native.members
        launcher = next((m for m in members if m.role == "launcher" and m.created), None)
        if launcher is None:
            return None
        worker = next((m for m in members if m.role == "worker" and m.created), None)
        return ProcessOwner(runner_instance_id=self.runner_id, job_name=self.native.job_name,
                            launcher=_birth(launcher), worker=_birth(worker) if worker else None,
                            members=tuple(_birth(m) for m in members if m.role == "member" and m.created),
                            observed_at=_now(), identity_uncertain=self.native.identity_uncertain,
                            unresolved_member_pids=tuple(sorted(self.native.unresolved_member_pids)),
                            runtime=self.runtime)

    def _sample(self):
        self.native.capture_members()
        owner = self._observation()
        if owner:
            self.index.update_owner(self.request, owner)

    def _next(self, deadline):
        while True:
            if self.cancel.is_set():
                raise WorkflowIndexError("cancelled")
            if time.monotonic() >= deadline:
                raise WorkflowIndexError("timeout")
            if self.failure.is_set():
                raise ProtocolError("invalid_io")
            self._sample()
            try:
                return self.frames.get(timeout=min(.05, max(0, deadline - time.monotonic())))
            except queue.Empty:
                pass

    def run(self):
        with self.lock:
            self._run()

    def _run(self):
        duration = self.request.profile.probe_seconds if self.request.kind == "probe_root" else self.request.profile.scan_seconds
        deadline = time.monotonic() + duration
        try:
            # Capture namespace/parent birth before creating any scanner. Failed
            # identity acquisition is a clean no-process-created refusal.
            self.runtime = RuntimeIdentity.model_validate(WindowsAPI().runtime_identity())
            self.native.start(*_launch())
            owner = self._observation()
            if owner:
                self.index.update_owner(self.request, owner)
            for target in (self._stdout, self._stderr, self._writer):
                thread = threading.Thread(target=target, name="filmpaw-scan-io", daemon=True)
                self.io.append(thread)
                thread.start()
            hello = self._next(deadline)
            if (not isinstance(hello, dict) or set(hello) != {*self.key, "type", "pid", "schema_version"}
                    or hello != {**self.key, "type": "hello", "schema_version": 1, "pid": hello.get("pid")}
                    or type(hello["schema_version"]) is not int
                    or any(type(hello[k]) is not type(v) for k, v in self.key.items())):
                raise ProtocolError("invalid_hello")
            self.native.capture_worker(hello["pid"])
            self._sample()
            self.index.authorize_go(self.request, self._observation())
            self.go.set()
            receiver = PayloadReader(self.key, total_limit=self.request.profile.total_bytes)
            complete = False
            while True:
                frame = self._next(deadline)
                if frame is None:
                    if not complete:
                        raise ProtocolError("short_eof")
                    break
                if complete:
                    raise ProtocolError("trailing_frame")
                if frame.get("type") == "error":
                    if set(frame) != {*self.key, "type", "code"} or any(frame.get(k) != v for k, v in self.key.items()):
                        raise ProtocolError("invalid_error")
                    allowed = {"offline", "permission_denied", "not_directory", "identity_changed", "identity_unknown", "partial"}
                    if frame["code"] not in allowed:
                        raise ProtocolError("invalid_error")
                    self.error = frame["code"]
                    complete = True
                    continue
                value = receiver.accept(frame)
                if value is not None:
                    self.snapshot = ScanSnapshot.model_validate_json(canonical(value))
                    from filmpaw_server.scan_worker import binding
                    if any(getattr(self.snapshot, k) != v for k, v in binding(self.request).items()):
                        raise ProtocolError("wrong_binding")
                    complete = True
            self.status = "complete" if self.snapshot is not None else "partial" if self.error == "partial" else "failed"
        except ProtocolError:
            self.error = "protocol_error"
        except WorkflowIndexError as error:
            self.error = error.code if error.code in {"timeout", "cancelled", "stale_binding"} else "storage_error"
            self.status = "cancelled" if self.error == "cancelled" else "failed"
        except OSError:
            self.error = "spawn_failed" if self.native.no_process_created else "native_failure"
        except Exception:
            # Never include exception text, request path or child output.
            self.error = "protocol_error"
        finally:
            self.stopping.set()
            if self.error:
                self.snapshot = None
            self._finish(time.monotonic() + self.request.profile.cleanup_seconds)

    def _finish(self, deadline):
        proof = self.native.settle(deadline, threads=tuple(self.io))
        owner = self._observation()
        try:
            if owner:
                self.index.update_owner(self.request, owner)
            exits = tuple(ProcessExit(birth=_birth(m), signaled=m.signaled) for m in self.native.members if m.created)
            exit_proof = ExitProof(attempt_id=self.request.attempt_id, owner_generation=self.request.owner_generation,
                                   processes=exits, job_empty=proof.job_empty, io_joined=proof.io_joined,
                                   handles_closed=proof.handles_closed, identity_uncertain=self.native.identity_uncertain,
                                   no_process_created=self.native.no_process_created,
                                   failure_codes=() if proof.released else ("unknown_owner",), checked_at=_now())
            outcome = ScanOutcome(status=self.status, owner_state="released" if exit_proof.released else "unknown_owner",
                                  snapshot=self.snapshot, exit_proof=exit_proof, error_code=self.error)
            self.result = self.index.finish(self.request, outcome)
        except Exception:
            self.result = None  # Retain the owner until the persistence boundary succeeds.
        return self.result is not None and self.result.owner_state == "released"

    def reconcile(self, deadline):
        with self.lock:
            return self._finish(deadline)


@dataclass(frozen=True)
class ShutdownOutcome:
    clean: bool


class ScanSupervisor:
    def __init__(self, index):
        self.index = index
        self.runner_id = str(uuid4())
        self._gate = threading.RLock()
        self._closing = False
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._attempts = {}
        self._recovery = {}
        self._auto_refresh = False

    def start(self, auto_refresh=False):
        with self._gate:
            if self._thread is not None or self._closing:
                return
            self._auto_refresh = auto_refresh
            self._thread = threading.Thread(target=self._dispatch, name="filmpaw-scans", daemon=True)
            self._thread.start()

    def _admit(self):
        if self._closing:
            raise WorkflowIndexError("scan_exit_pending", 409)

    def request(self, root_id, *, kind="scan_index", request_id, manual=False):
        with self._gate:
            self._admit()
            ticket = self.index.reserve_scan(root_id, request_id=request_id, kind=kind, manual=manual)
            self._wake.set()
            return ticket

    def request_probe(self, draft, *, request_id, manual=False):
        with self._gate:
            self._admit()
            ticket = self.index.reserve_probe(draft, request_id=request_id, manual=manual)
            self._wake.set()
            return ticket

    def _dispatch(self):
        # Startup recovery precedes any new launch. Native queries run in this
        # dispatcher, never in an SQLite transaction or the ASGI event loop.
        with self._gate:
            try:
                self._recover_persisted(time.monotonic() + self.index.profile.cleanup_seconds)
            except Exception:
                pass  # Persistent unknown owners still fence admission.
        while not self._stop.is_set():
            with self._gate:
                if not self._closing:
                    try:
                        for key, attempt in tuple(self._attempts.items()):
                            if not attempt.thread.is_alive() and attempt.result and attempt.result.owner_state == "released":
                                attempt.thread.join()
                                del self._attempts[key]
                        if self._auto_refresh:
                            for kind, roots in (("probe_root", self.index.probe_roots()), ("scan_index", self.index.refresh_roots())):
                                for root in roots:
                                    try:
                                        self.index.reserve_scan(root, request_id=str(uuid4()), kind=kind, manual=False)
                                    except WorkflowIndexError:
                                        break
                        for ticket in self.index.queued_tickets():
                            try:
                                request = self.index.begin_attempt(ticket)
                            except WorkflowIndexError as exc:
                                if exc.code != "probe_required":
                                    continue
                                try:
                                    request = self.index.start_recovery_probe(ticket)
                                except WorkflowIndexError:
                                    continue
                            attempt = _Attempt(self.index, request, self.runner_id)
                            self._attempts[request.scan_id] = attempt
                            attempt.thread.start()
                    except Exception:
                        pass  # Repository state remains authoritative; never launch outside its reservation.
            self._wake.wait(.1)
            self._wake.clear()

    async def wait(self, ticket, *, deadline):
        while True:
            result = await asyncio.to_thread(self.index.get_scan, ticket.scan_id)
            if result.status not in {"queued", "starting", "running"} or time.monotonic() >= deadline:
                return result
            await asyncio.sleep(.02)

    def reconcile(self, host_id):
        with self._gate:
            deadline = time.monotonic() + self.index.profile.cleanup_seconds
            for attempt in self._attempts.values():
                if attempt.request.host_id == host_id and not attempt.thread.is_alive():
                    attempt.reconcile(deadline)
            self._recover_persisted(deadline, host_id=host_id)
            return ShutdownOutcome(not any(p.request.host_id == host_id for p in self.index.pending_owners()))

    def _recover_persisted(self, deadline, host_id=None):
        for pending in self.index.pending_owners():
            request = pending.request
            if (request.scan_id in self._attempts or (host_id is not None and request.host_id != host_id)
                    or time.monotonic() >= deadline):
                continue
            inspection = self._recovery.get(request.scan_id)
            if inspection is None:
                inspection = RecoveryInspection(pending)
                self._recovery[request.scan_id] = inspection
            proof = inspection.check(deadline)
            if proof is not None:
                # This separate command can only end ownership, never publish
                # an interrupted scan's snapshot. On storage failure keep it.
                self.index.recover_owner(request, proof)
                del self._recovery[request.scan_id]

    def prepare_exit(self, callback):
        with self._gate:
            prior_closing = self._closing
            self._closing = True
            safe = False
            try:
                for attempt in self._attempts.values():
                    if attempt.thread.is_alive():
                        attempt.cancel.set()
                        raise WorkflowIndexError("scan_exit_pending", 409)
                deadline = time.monotonic() + self.index.profile.cleanup_seconds
                for attempt in self._attempts.values():
                    if not attempt.result or attempt.result.owner_state != "released":
                        attempt.reconcile(deadline)
                self._recover_persisted(deadline)
                if self.index.pending_owners():
                    raise WorkflowIndexError("scan_exit_pending", 409)
                result = callback()
                safe = result.get("safe") is True
                return result
            except WorkflowIndexError:
                raise
            except Exception:
                raise WorkflowIndexError("scan_exit_pending", 409) from None
            finally:
                if not safe:
                    self._closing = prior_closing
                    self._wake.set()

    def close(self):
        with self._gate:
            self._closing = True
            self._stop.set()
            self._wake.set()
            attempts = tuple(self._attempts.values())
            for attempt in attempts:
                attempt.cancel.set()
        deadline = time.monotonic() + self.index.profile.cleanup_seconds + .5
        if self._thread:
            self._thread.join(max(0, deadline - time.monotonic()))
        for attempt in attempts:
            attempt.thread.join(max(0, deadline - time.monotonic()))
        with self._gate:
            try:
                if any(a.thread.is_alive() for a in attempts):
                    return ShutdownOutcome(False)
                for attempt in attempts:
                    if not attempt.result or attempt.result.owner_state != "released":
                        attempt.reconcile(deadline)
                self._recover_persisted(deadline)
                return ShutdownOutcome(not self.index.pending_owners() and not (self._thread and self._thread.is_alive()))
            except Exception:
                return ShutdownOutcome(False)

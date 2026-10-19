"""Read-only, bounded reconciliation of one persisted scanner owner.

Missing historical identity stays unknown. This module never starts, kills or
attaches a process, creates a Job, accesses a NAS, or opens the library database.
"""

import re
import time
from datetime import datetime, timezone

from pydantic import ValidationError

from filmpaw_server.scan_native import WindowsAPI
from filmpaw_server.workflow_models import RecoveredBirth, RecoveredOwnerProof, RuntimeIdentity, digest


class RecoveryInspection:
    def __init__(self, pending, api=None):
        self.pending = pending
        self.api = api
        self.handles = set()

    @property
    def closed(self):
        return not self.handles and not getattr(self.api, "snapshot_handles", ())

    def _close(self):
        snapshots = getattr(self.api, "snapshot_handles", set())
        for handle in tuple(self.handles | snapshots):
            try:
                self.api.close(handle)
                self.handles.discard(handle)
                snapshots.discard(handle)
            except OSError:
                pass  # Retain this exact acquired handle for the next bounded call.
        return self.closed

    @staticmethod
    def _budget(deadline):
        if time.monotonic() >= deadline:
            raise OSError("scanner_recovery_deadline")

    def _birth(self, pid, created, role, deadline):
        self._budget(deadline)
        if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF or not 0 < created < 1 << 64:
            raise OSError("scanner_recovery_invalid_birth")
        observed = None
        try:
            handle = self.api.open_process(pid)
        except OSError as error:
            if getattr(error, "winerror", None) != 87:
                raise
            # ERROR_INVALID_PARAMETER alone is never process-exit evidence.
            ids = self.api.process_ids()
            self._budget(deadline)
            if pid in ids:
                raise OSError("scanner_recovery_pid_still_present")
            disposition = "pid_absent"
        else:
            self.handles.add(handle)
            actual_pid, observed = self.api.process_birth(handle)
            if actual_pid != pid:
                raise OSError("scanner_recovery_wrong_pid")
            if observed != created:
                disposition = "birth_reused"
            elif self.api.wait(handle, 0):
                disposition = "handle_signaled"
            else:
                raise OSError("scanner_recovery_still_running")
        return RecoveredBirth(pid=pid, expected_created_filetime_100ns=created, role=role,
                              disposition=disposition, observed_created_filetime_100ns=observed)

    def check(self, deadline):
        owner, request = self.pending.owner, self.pending.request
        if not self._close():
            return None
        if (owner is None or owner.runtime is None or owner.identity_uncertain or owner.unresolved_member_pids
                or not re.fullmatch(r"Local\\FilmpawScan-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", owner.job_name)):
            return None
        candidate = None
        try:
            self._budget(deadline)
            self.api = self.api or WindowsAPI()
            current = RuntimeIdentity.model_validate(self.api.runtime_identity())
            old = owner.runtime
            if ((current.machine_digest, current.session_id) != (old.machine_digest, old.session_id)
                    or (current.pid, current.created_filetime_100ns) == (old.pid, old.created_filetime_100ns)):
                return None
            runtime = self._birth(old.pid, old.created_filetime_100ns, "runtime", deadline)
            births = [owner.launcher, *([owner.worker] if owner.worker else []), *owner.members]
            if len(births) > 34:
                return None
            processes = tuple(self._birth(b.pid, b.created_filetime_100ns, b.role, deadline) for b in births)
            self._budget(deadline)
            try:
                job = self.api.open_job(owner.job_name)
            except OSError as error:
                if getattr(error, "winerror", None) != 2:
                    raise
                job_state = "absent"
            else:
                self.handles.add(job)
                if self.api.active(job) != 0 or self.api.members(job):
                    return None
                job_state = "empty"
            self._budget(deadline)
            candidate = RecoveredOwnerProof(scan_id=request.scan_id, attempt_id=request.attempt_id,
                owner_generation=request.owner_generation, request_digest=digest(request), owner_digest=digest(owner),
                inspector_runtime=current, original_runtime=old, runtime=runtime, processes=processes,
                job_name=owner.job_name, job_state=job_state, handles_closed=True, checked_at=datetime.now(timezone.utc))
        except (OSError, ValidationError, ValueError):
            candidate = None
        finally:
            closed = self._close()
        return candidate if closed else None

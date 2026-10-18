"""One sidecar owns a library's recovery and transfer journal at a time."""

import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def library_session(db_path: Path):
    path = db_path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep this inode after release. Unlinking a lock file would let a third
    # process lock a new inode while a second still owns the original one.
    with path.with_name(path.name + ".session.lock").open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("该片库已由另一个 Filmpaw 会话打开，请先关闭另一个窗口") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

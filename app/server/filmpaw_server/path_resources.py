"""Read-only directory identities for actor ownership, including Windows aliases.

These short-lived metadata handles do not authorize filesystem mutations. The
transfer engine revalidates its frozen identities under its existing guards.
"""

import ctypes
import os
import stat
import sys
from pathlib import Path

from filmpaw_server.windows_identity import directory_identity


def _info(path: Path) -> dict:
    if sys.platform != "win32":
        value = path.lstat()
        if not stat.S_ISDIR(value.st_mode) or stat.S_ISLNK(value.st_mode):
            raise OSError("Actor path is not a plain directory")
        return {"identity": [value.st_dev, str(value.st_ino)], "path": str(path.resolve())}
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    handle = create(str(path), 0x80, 7, None, 3, 0x02200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        identity = directory_identity(handle, kernel=kernel, include_alias=True)
        final = kernel.GetFinalPathNameByHandleW
        final.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        final.restype = wintypes.DWORD
        size = 512
        while size <= 32768:
            buffer = ctypes.create_unicode_buffer(size)
            count = final(handle, buffer, size, 0)
            if not count:
                raise ctypes.WinError(ctypes.get_last_error())
            if count < size:
                canonical = buffer.value
                if canonical.startswith("\\\\?\\UNC\\"):
                    canonical = "\\\\" + canonical[8:]
                elif canonical.startswith("\\\\?\\"):
                    canonical = canonical[4:]
                return {**identity, "path": canonical}
            size = count + 1
        raise OSError("Actor canonical path is too long")
    finally:
        close(handle)


def capture(path: Path) -> dict:
    root = _info(path)
    ancestors = [_info(parent) for parent in Path(root["path"]).parents]
    root["ancestors"] = [item["identity"] for item in ancestors]
    root["ancestor_aliases"] = [item.get("identity_aliases", []) for item in ancestors]
    return root


def _identities_overlap(first: list, second: list) -> bool:
    if any(a == b for a in first for b in second):
        return True
    def namespace(identity):
        return "legacy64" if identity[0] == "legacy64" else "native"
    # Without a common, verified namespace we cannot prove that two aliases
    # refer to independent directories. Do not guess by truncating either ID.
    return not ({namespace(item) for item in first} & {namespace(item) for item in second})


def _identity_sets(resource: dict) -> tuple[list, list]:
    root = [resource["identity"], *resource.get("identity_aliases", [])]
    aliases = resource.get("ancestor_aliases", [])
    ancestors = [[identity, *(aliases[index] if index < len(aliases) else [])]
                 for index, identity in enumerate(resource["ancestors"])]
    return root, ancestors


def overlaps(first: dict, second: dict) -> bool:
    first_ids, first_ancestors = _identity_sets(first)
    second_ids, second_ancestors = _identity_sets(second)
    if (_identities_overlap(first_ids, second_ids)
            or any(_identities_overlap(first_ids, ancestor) for ancestor in second_ancestors)
            or any(_identities_overlap(second_ids, ancestor) for ancestor in first_ancestors)):
        return True
    a, b = (os.path.normcase(os.path.normpath(item["path"])) for item in (first, second))
    try:
        return os.path.commonpath([a, b]) in {a, b}
    except ValueError:
        return False

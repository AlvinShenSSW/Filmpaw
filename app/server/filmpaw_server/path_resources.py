"""Read-only directory identities for actor ownership, including Windows aliases.

These short-lived metadata handles do not authorize filesystem mutations. The
transfer engine revalidates its frozen identities under its existing guards.
"""

import ctypes
import os
import stat
import sys
from pathlib import Path


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
        class FileIdInfo(ctypes.Structure):
            _fields_ = [("volume", ctypes.c_ulonglong), ("file_id", ctypes.c_ubyte * 16)]
        class AttributeInfo(ctypes.Structure):
            _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]
        identity, attrs = FileIdInfo(), AttributeInfo()
        get = kernel.GetFileInformationByHandleEx
        get.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        get.restype = wintypes.BOOL
        for kind, value in [(18, identity), (9, attrs)]:
            if not get(handle, kind, ctypes.byref(value), ctypes.sizeof(value)):
                raise ctypes.WinError(ctypes.get_last_error())
        if attrs.attributes & 0x400 or not attrs.attributes & 0x10 or not any(identity.file_id):
            raise OSError("Actor directory identity is unavailable or is a reparse point")
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
                return {"identity": [identity.volume, bytes(identity.file_id).hex()], "path": canonical}
            size = count + 1
        raise OSError("Actor canonical path is too long")
    finally:
        close(handle)


def capture(path: Path) -> dict:
    root = _info(path)
    root["ancestors"] = [_info(parent)["identity"] for parent in Path(root["path"]).parents]
    return root


def overlaps(first: dict, second: dict) -> bool:
    if (first["identity"] == second["identity"]
            or first["identity"] in second["ancestors"]
            or second["identity"] in first["ancestors"]):
        return True
    a, b = (os.path.normcase(os.path.normpath(item["path"])) for item in (first, second))
    try:
        return os.path.commonpath([a, b]) in {a, b}
    except ValueError:
        return False

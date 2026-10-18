"""Directory identities read from one Windows handle, including older SMB servers.

The legacy namespace is deliberately distinct: neither the 128-bit file ID nor
the 64-bit volume ID may be truncated to manufacture a compatible identity.
"""

import ctypes
from ctypes import wintypes


UNSUPPORTED_FILE_ID = {1, 50, 87}


class NotPlainDirectory(OSError):
    pass


class _AttributeInfo(ctypes.Structure):
    _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]


class _FileIdInfo(ctypes.Structure):
    _fields_ = [("volume", ctypes.c_ulonglong), ("file_id", ctypes.c_ubyte * 16)]


class _LegacyInfo(ctypes.Structure):
    _fields_ = [
        ("attributes", wintypes.DWORD),
        ("created", wintypes.FILETIME), ("accessed", wintypes.FILETIME),
        ("written", wintypes.FILETIME), ("volume", wintypes.DWORD),
        ("size_high", wintypes.DWORD), ("size_low", wintypes.DWORD),
        ("links", wintypes.DWORD),
        ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD),
    ]


def _plain(attributes: int) -> None:
    if attributes & 0x400 or not attributes & 0x10:
        raise NotPlainDirectory("不是普通目录或属于重解析点，已保留")


def _legacy_identity(kernel, handle) -> list:
    filesystem = ctypes.create_unicode_buffer(256)
    volume = kernel.GetVolumeInformationByHandleW
    volume.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD,
                       ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                       ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD]
    volume.restype = wintypes.BOOL
    if not volume(handle, None, 0, None, None, None, filesystem, len(filesystem)):
        raise ctypes.WinError(ctypes.get_last_error())
    # ReFS explicitly does not guarantee unique 64-bit IDs. Unknown filesystems
    # must not inherit NTFS's identity guarantee merely because this API exists.
    if filesystem.value.upper() != "NTFS":
        raise OSError("文件系统不支持可靠的兼容目录身份，已保留")
    legacy = _LegacyInfo()
    get = kernel.GetFileInformationByHandle
    get.argtypes = [wintypes.HANDLE, ctypes.POINTER(_LegacyInfo)]
    get.restype = wintypes.BOOL
    if not get(handle, ctypes.byref(legacy)):
        raise ctypes.WinError(ctypes.get_last_error())
    _plain(legacy.attributes)
    index = (legacy.index_high << 32) | legacy.index_low
    if index in {0, 0xFFFFFFFFFFFFFFFF}:
        raise OSError("无法取得可靠的兼容目录身份，已保留")
    return ["legacy64", legacy.volume, index]


def directory_identity(handle, *, kernel=None, include_alias: bool = False) -> dict:
    """Never reopen by path. Optional aliases authorize comparisons, not deletes."""
    if kernel is None:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get = kernel.GetFileInformationByHandleEx
    get.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    get.restype = wintypes.BOOL
    attributes = _AttributeInfo()
    if not get(handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)):
        raise ctypes.WinError(ctypes.get_last_error())
    _plain(attributes.attributes)
    native = _FileIdInfo()
    aliases = []
    if get(handle, 18, ctypes.byref(native), ctypes.sizeof(native)):
        identifier = bytes(native.file_id)
        if identifier in {bytes(16), b"\xff" * 16}:
            raise OSError("无法取得可靠的目录身份，已保留")
        identity = [native.volume, identifier.hex()]
        if include_alias:
            try:
                aliases.append(_legacy_identity(kernel, handle))
            except NotPlainDirectory:
                raise
            except OSError:
                # A valid native ID still permits browsing and native/native
                # comparisons. Missing bridges fail closed for mixed modes.
                pass
    else:
        error = ctypes.get_last_error()
        if error not in UNSUPPORTED_FILE_ID:
            raise ctypes.WinError(error)
        identity = _legacy_identity(kernel, handle)
    return {"identity": identity, "identity_aliases": aliases,
            "attributes": attributes.attributes}

"""Retry hints for read-only directory discovery, never transfer operations."""

import errno


# Windows network failures sometimes map to ENOENT/EACCES. Check WinError
# first so an unavailable share is not mistaken for a deleted actor directory.
_TEMPORARY_WINDOWS = {32, 33, 51, 53, 54, 55, 59, 64, 67, 71, 121, 170, 1231, 1232, 1236, 1237, 1460}
_TEMPORARY_ERRNO = {
    errno.EAGAIN, errno.EBUSY, errno.EINTR, errno.ECONNABORTED,
    errno.ECONNRESET, errno.ECONNREFUSED, errno.ENETDOWN,
    errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENOTCONN, errno.ETIMEDOUT,
}


def directory_error_retryable(error: BaseException | None) -> bool:
    if not isinstance(error, OSError):
        return False  # Includes path-identity/reparse/binding safety refusals.
    winerror = getattr(error, "winerror", None)
    if winerror is not None:
        return winerror in _TEMPORARY_WINDOWS
    return isinstance(error, TimeoutError) or error.errno in _TEMPORARY_ERRNO

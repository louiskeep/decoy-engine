"""Read a whole file through the installed policy: the one helper small readers share."""

from __future__ import annotations

from decoy_engine.fileaccess._entry import current, is_ambient
from decoy_engine.fileaccess._errors import encode_field_key, passes_by_type, resurface_scrubbed
from decoy_engine.fileaccess._types import FileRequest, FileRole


def read_all(role: FileRole, locator: str, field_key: str, limit: int | None = None) -> bytes:
    """The bytes of one file the installed policy authorizes for `role`.

    Errors from a local read are the plain `OSError` the caller always saw, so CLI text is
    unchanged; a strict policy raises the one denial instead."""
    handle = current().open_read(FileRequest(role, "read", locator, field_key))
    try:
        return handle.read_all(limit)
    finally:
        handle.close()


def read_text(role: FileRole, locator: str, field_key: str, encoding: str = "utf-8") -> str:
    """Like `read_all`, decoded in text mode so newline translation matches `open(..., "r")`."""
    handle = current().open_read(FileRequest(role, "read", locator, field_key))
    try:
        with handle.open_text(encoding) as stream:
            return stream.read()
    finally:
        handle.close()


def shown(locator: str, field_key: str) -> str:
    """What an engine message may call a file: the locator under the ambient CLI policy (so
    CLI text is unchanged), the encoded config position under any other."""
    return locator if is_ambient() else encode_field_key(field_key)


def keeps_class(exc: BaseException) -> bool:
    """True when a handler that translates, stores or swallows an exception raised from a file
    call must re-raise it unchanged under a strict policy: a policy decision, a typed read
    error, and the failures callers classify by type or by the memory predicate (memory, a
    full disk or quota). Always False under the ambient policy, whose handlers keep today's
    behavior."""
    if is_ambient():
        return False
    if isinstance(exc, Exception) and passes_by_type(exc):
        return True
    from decoy_engine.fileaccess._types import (
        FileAccessDenied,
        FileAccessReadError,
        FileAccessUnavailable,
    )

    if isinstance(exc, (FileAccessDenied, FileAccessReadError, FileAccessUnavailable)):
        return True
    return isinstance(exc, Exception) and resurface_scrubbed(exc) is not None

"""Read a whole file through the installed policy: the one helper small readers share."""

from __future__ import annotations

from decoy_engine.fileaccess._entry import current
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

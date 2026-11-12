"""Serialization adapter: JSON, YAML, text and bytes over policy capabilities."""

from __future__ import annotations

import errno
import json
import os
from typing import Any

import yaml

from decoy_engine.fileaccess._errors import guarded
from decoy_engine.fileaccess._types import FileHandle, StageDir


def _strict(handle: FileHandle) -> bool:
    return bool(getattr(handle, "strict", True))


def read_all(handle: FileHandle, limit: int | None = None) -> bytes:
    return guarded(handle.display, _strict(handle), handle.read_all, limit)


def read_text(handle: FileHandle, encoding: str = "utf-8") -> str:
    return guarded(handle.display, _strict(handle), read_all(handle).decode, encoding)


def json_load(handle: FileHandle) -> Any:
    return guarded(handle.display, _strict(handle), json.loads, read_all(handle))


def yaml_safe_load(handle: FileHandle) -> Any:
    """Parse YAML from an open text handle. Under the ambient policy a YAML error names the file
    as it always did; under a strict policy `guarded` replaces it with a locator-free
    `FileAccessReadError`."""
    with handle.open_text() as fh:
        return guarded(handle.display, _strict(handle), yaml.safe_load, fh)


def write_text(txn: StageDir, component: str, text: str, encoding: str = "utf-8") -> None:
    """Write `text` as a new file of the stage."""
    with os.fdopen(txn.create_file(component), "wb") as fh:
        fh.write(text.encode(encoding))


def write_bytes(txn: StageDir, component: str, data: bytes) -> None:
    with os.fdopen(txn.create_file(component), "wb") as fh:
        fh.write(data)


def write_durable(fd: int, data: bytes) -> None:
    """Write every byte to a stage descriptor, fsync it, close it; any failure is raised.

    The raw descriptor (not a buffered handle) so a short write is retried and a write, fsync
    or close error reaches the caller instead of being lost in a buffer flush (POSIX; the
    durable-rename sequence of SQLite and PostgreSQL)."""
    failed = True
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError(errno.EIO, os.strerror(errno.EIO))
            view = view[written:]
        os.fsync(fd)
        failed = False
    finally:
        if failed:
            try:
                os.close(fd)
            except OSError:
                pass
    os.close(fd)

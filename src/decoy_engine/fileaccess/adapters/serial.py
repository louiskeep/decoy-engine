"""Serialization adapter: JSON, YAML, text and bytes over policy capabilities."""

from __future__ import annotations

import json
import os
from typing import Any

import yaml

from decoy_engine.fileaccess._errors import guarded
from decoy_engine.fileaccess._types import FileHandle, OutputTxn


def _strict(handle: FileHandle) -> bool:
    return bool(getattr(handle, "strict", True))


def read_all(handle: FileHandle, limit: int | None = None) -> bytes:
    return guarded(handle.display, _strict(handle), handle.read_all, limit)


def read_text(handle: FileHandle, encoding: str = "utf-8") -> str:
    return read_all(handle).decode(encoding)


def json_load(handle: FileHandle) -> Any:
    return guarded(handle.display, _strict(handle), json.loads, read_all(handle))


def yaml_safe_load(handle: FileHandle) -> Any:
    """Parse as UTF-8 text from a named file object, so a YAML error names the file as it
    always did (the display, under a strict policy)."""
    with handle.open_text() as fh:
        return guarded(handle.display, _strict(handle), yaml.safe_load, fh)


def write_text(txn: OutputTxn, component: str, text: str, encoding: str = "utf-8") -> None:
    """Write `text` as a new file of the stage."""
    with os.fdopen(txn.create_file(component), "wb") as fh:
        fh.write(text.encode(encoding))


def write_bytes(txn: OutputTxn, component: str, data: bytes) -> None:
    with os.fdopen(txn.create_file(component), "wb") as fh:
        fh.write(data)

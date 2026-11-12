"""A `FileHandle` over bytes the engine already holds, for the pandas and Arrow adapters.

A pinned artifact (a lookup file) is read once through the policy and kept in the plan. Parsing
it later must not open the path again, so the adapters get this handle instead: it stands for
those bytes, names only a config position, and is always strict, so a library failure becomes the
one value-free `FileAccessReadError` whatever policy read the file.
"""

from __future__ import annotations

import io
import os
from collections.abc import Callable
from typing import Any, BinaryIO, TextIO


class MemoryHandle:
    """In-memory stand-in for an open, authorized file. Nothing here touches a file system."""

    strict = True

    def __init__(self, data: bytes, display: str) -> None:
        self._data = data
        self.display = display

    def fileno(self) -> int:
        raise io.UnsupportedOperation("a memory handle has no descriptor")

    def library_path(self) -> Any:
        """A fresh buffer per call: pandas and pyarrow accept one where they accept a path."""
        return io.BytesIO(self._data)

    def stat(self) -> os.stat_result:
        return os.stat_result((0o100444, 0, 0, 1, 0, 0, len(self._data), 0, 0, 0))

    def identity(self) -> str:
        return self.display

    def read_all(self, limit: int | None = None) -> bytes:
        return self._data if limit is None else self._data[:limit]

    def open_binary(self, opener: Callable[..., BinaryIO] | None = None) -> BinaryIO:
        return io.BytesIO(self._data)

    def open_text(self, encoding: str = "utf-8") -> TextIO:
        return io.TextIOWrapper(io.BytesIO(self._data), encoding=encoding)

    def close(self) -> None:
        return None

    def __enter__(self) -> MemoryHandle:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

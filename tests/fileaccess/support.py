"""Test support: a rooted policy over a temporary tree, and a recording policy.

`RootRelative` serves the absolute temporary paths the existing job fixtures use by stripping
the root prefix and then applying the real `openat2` confinement, so a job runs end to end
under `RootedFileAccess`. `RecordingFileAccess` wraps any policy and records every request it
decides, so a test can show every file access went through a capability.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Any

from decoy_engine.fileaccess import (
    FileAccess,
    FileAccessDenied,
    FileRequest,
    FileRole,
    Root,
    RootedFileAccess,
)

ALL_ACCESS = frozenset({"read", "stat", "read_dir", "write"})


class RootRelative(RootedFileAccess):
    """A `RootedFileAccess` over one directory that accepts the absolute paths beneath it."""

    def __init__(self, root: Path) -> None:
        self._root_path = os.path.realpath(root)
        self.root_fd = os.open(self._root_path, os.O_RDONLY | os.O_DIRECTORY)
        super().__init__([Root("tmp", self.root_fd, frozenset(FileRole), ALL_ACCESS)])
        self._prefix = self._root_path.rstrip("/") + "/"

    def select(self, req: FileRequest) -> tuple[Root, str]:
        locator = req.locator
        if isinstance(locator, str) and os.path.isabs(locator):
            canonical = os.path.normpath(locator)
            if canonical.startswith(self._prefix):
                req = dataclasses.replace(req, locator=canonical[len(self._prefix) :])
        return super().select(req)

    def close(self) -> None:
        os.close(self.root_fd)


class RecordingFileAccess:
    """Delegates to `inner` and records `(role, access, field_key, outcome)` per request."""

    def __init__(self, inner: FileAccess) -> None:
        self.inner = inner
        self.requests: list[tuple[FileRole, str, str, str]] = []

    def _record(self, req: FileRequest, outcome: str) -> None:
        self.requests.append((req.role, req.access, req.field_key, outcome))

    def _do(self, name: str, req: FileRequest) -> Any:
        try:
            result = getattr(self.inner, name)(req)
        except FileAccessDenied:
            self._record(req, "denied")
            raise
        self._record(req, "granted")
        return result

    def open_read(self, req: FileRequest) -> Any:
        return self._do("open_read", req)

    def open_dir(self, req: FileRequest) -> Any:
        return self._do("open_dir", req)

    def stat(self, req: FileRequest) -> Any:
        return self._do("stat", req)

    def output_dir(self, req: FileRequest) -> Any:
        return self._do("output_dir", req)

    def allows_subprocess(self) -> bool:
        return self.inner.allows_subprocess()

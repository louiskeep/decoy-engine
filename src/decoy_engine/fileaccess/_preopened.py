"""`PreopenedFileAccess`: serves only requests that match a descriptor opened in advance.

Used by an engine-spawned child that cannot resolve any path: the parent opened each input
through its own policy and passed the descriptors, the child serves exactly those
`(role, field_key)` pairs and denies everything else.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from decoy_engine.fileaccess._errors import deny
from decoy_engine.fileaccess._rooted import FdDirHandle, FdHandle
from decoy_engine.fileaccess._types import FileRequest, FileRole, OutputDir

Grant = tuple[FileRole, str]


class PreopenedFileAccess:
    def __init__(
        self,
        files: Mapping[Grant, int] | None = None,
        dirs: Mapping[Grant, int] | None = None,
    ) -> None:
        self._files = dict(files or {})
        self._dirs = dict(dirs or {})

    def allows_subprocess(self) -> bool:
        return False

    def open_read(self, req: FileRequest) -> FdHandle:
        fd = self._files.get((req.role, req.field_key))
        if fd is None or req.access not in ("read", "stat"):
            raise deny(req.field_key)
        return FdHandle(os.dup(fd), req.field_key)

    def open_dir(self, req: FileRequest) -> FdDirHandle:
        fd = self._dirs.get((req.role, req.field_key))
        if fd is None:
            raise deny(req.field_key)
        return FdDirHandle(os.dup(fd), req.field_key)

    def stat(self, req: FileRequest) -> os.stat_result:
        fd = self._files.get((req.role, req.field_key))
        if fd is None:
            fd = self._dirs.get((req.role, req.field_key))
        if fd is None:
            raise deny(req.field_key)
        return os.fstat(fd)

    def output_dir(self, req: FileRequest) -> OutputDir:
        raise deny(req.field_key)


__all__ = ["PreopenedFileAccess"]

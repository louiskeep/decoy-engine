"""Base for file-backed `ProfileSource`s that open their file through a file-access policy."""

from __future__ import annotations

from pathlib import Path

from decoy_engine import fileaccess
from decoy_engine.fileaccess import FileAccess, FileHandle, FileRequest, FileRole


class _PolicyBound:
    """What a file source needs to ask the policy it was built under for its file."""

    path: Path
    access: FileAccess | None
    field_key: str

    def _handle(self) -> FileHandle:
        if self.access is None:  # pragma: no cover - set in __post_init__
            raise fileaccess.FileAccessUnavailable("no file access policy")
        return self.access.open_read(
            FileRequest(FileRole.SOURCE, "read", str(self.path), self.field_key)
        )

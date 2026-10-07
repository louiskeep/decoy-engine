"""The out-of-core runner's scratch root: engine-created, or one a caller names (ambient only)."""

from __future__ import annotations

import os
import tempfile
import weakref
from pathlib import Path

from decoy_engine import fileaccess
from decoy_engine.fileaccess import FileRequest, FileRole, Root, RootedFileAccess
from decoy_engine.fileaccess._rooted import lexical_ok

_ISSUER = object()


def scratch_root(temp_dir: Path | None) -> Path:
    """The private (0700) directory the runner spills into.

    With no `temp_dir` the engine makes its own. A directory the caller names is a caller path,
    which only the ambient policy follows, so any other policy is refused before any I/O."""
    if temp_dir is not None:
        fileaccess.require_ambient("run_fk_out_of_core with a caller-named temp_dir")
    root = Path(tempfile.mkdtemp(prefix="decoy-ooc-")) if temp_dir is None else temp_dir
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return root


class ScratchAccess(RootedFileAccess):
    """Read-only access to files beneath ONE engine-created scratch directory.

    Built only by `scratch_access_for` (the issuer token), over a descriptor the engine opened
    on a directory it made itself, so a source over a staged file never reaches the file
    system through an ambient path and cannot name anything outside that directory."""

    def __init__(self, directory: Path, *, issuer: object) -> None:
        if issuer is not _ISSUER:
            raise TypeError("ScratchAccess is issued by scratch_access_for")
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self._finalizer = weakref.finalize(self, os.close, fd)
        self._prefix = os.path.abspath(directory).rstrip("/") + "/"
        super().__init__(
            [Root("scratch", fd, frozenset({FileRole.SOURCE}), frozenset({"read", "stat"}))],
            allow_portable_fallback=True,
        )

    def select(self, req: FileRequest) -> tuple[Root, str]:
        locator = req.locator
        if lexical_ok(locator):
            canonical = os.path.normpath(locator)
            if canonical.startswith(self._prefix):
                req = FileRequest(
                    req.role, req.access, canonical[len(self._prefix) :], req.field_key
                )
        return super().select(req)


def scratch_access_for(directory: Path) -> ScratchAccess:
    """The reader for files the engine wrote under `directory`, a directory it created."""
    return ScratchAccess(directory, issuer=_ISSUER)

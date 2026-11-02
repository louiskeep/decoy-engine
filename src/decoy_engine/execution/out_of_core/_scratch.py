"""The out-of-core runner's scratch root: engine-created, or one a caller names (ambient only)."""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from decoy_engine import fileaccess
from decoy_engine.fileaccess import FileRequest, FileRole, Root, RootedFileAccess
from decoy_engine.fileaccess._rooted import lexical_ok

_ISSUER = object()
# Per-run roots `engine_scratch_root` made and has not yet removed: a temp_dir naming one of
# these is the engine's own directory, not a caller path.
_ENGINE_ROOTS: set[str] = set()
_ENGINE_ROOTS_LOCK = threading.Lock()


def scratch_root(temp_dir: Path | None) -> Path:
    """The private (0700) directory the runner spills into.

    With no `temp_dir` the engine makes its own. A directory the caller names is a caller path,
    which only the ambient policy follows, so any other policy is refused before any I/O. A root
    `engine_scratch_root` issued is the engine's own and is taken under every policy."""
    if temp_dir is not None and not _is_engine_root(temp_dir):
        fileaccess.require_ambient("run_fk_out_of_core with a caller-named temp_dir")
    root = Path(tempfile.mkdtemp(prefix="decoy-ooc-")) if temp_dir is None else temp_dir
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return root


def _is_engine_root(path: Path) -> bool:
    with _ENGINE_ROOTS_LOCK:
        return os.path.abspath(path) in _ENGINE_ROOTS


@contextmanager
def engine_scratch_root(parent: Path | None) -> Iterator[Path]:
    """A private per-run root the engine creates, removed on every exit.

    Under a caller-named `parent` (ambient policy only, as for `scratch_root`) or the system temp
    directory. While open, `scratch_root` accepts it as the run's `temp_dir` under any policy, so
    a staged copy and the route's spill share one root and one temp-disk budget."""
    if parent is not None:
        fileaccess.require_ambient("run_fk_out_of_core with a caller-named temp_dir")
        Path(parent).mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="decoy-ooc-run-", dir=parent))  # created 0700
    key = os.path.abspath(root)
    with _ENGINE_ROOTS_LOCK:
        _ENGINE_ROOTS.add(key)
    try:
        yield root
    finally:
        with _ENGINE_ROOTS_LOCK:
            _ENGINE_ROOTS.discard(key)
        shutil.rmtree(root, ignore_errors=True)


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

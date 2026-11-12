"""The out-of-core runner's scratch root: engine-created, or one a caller names (ambient only)."""

from __future__ import annotations

import os
import weakref
from contextlib import AbstractContextManager, ExitStack
from pathlib import Path
from typing import BinaryIO

from decoy_engine.fileaccess import (
    FileRequest,
    FileRole,
    Root,
    RootedFileAccess,
    open_protected_dir,
    protected_scratch_dir,
)
from decoy_engine.fileaccess._rooted import lexical_ok

_ISSUER = object()


def scratch_root(temp_dir: Path | None, stack: ExitStack | None = None) -> Path:
    """The private (0700) directory the runner spills into.

    With no `temp_dir` the engine makes its own, owned by `stack` (required then) and removed
    when the stack closes. A directory the caller names is a caller path, which only the ambient
    policy follows, so any other policy is refused before any I/O. A root `engine_scratch_root`
    or an earlier `scratch_root(None)` issued is the engine's own and is taken under every
    policy."""
    if temp_dir is not None:
        return open_protected_dir(temp_dir, create=True, parents=True)
    if stack is None:
        raise TypeError("scratch_root needs an ExitStack to own an engine-made root")
    return stack.enter_context(protected_scratch_dir(prefix="decoy-ooc-"))


def engine_scratch_root(parent: Path | None) -> AbstractContextManager[Path]:
    """A private per-run root the engine creates, removed on every exit.

    Under a caller-named `parent` (ambient policy only, as for `scratch_root`) or the system temp
    directory. While open, `scratch_root` accepts it as the run's `temp_dir` under any policy, so
    a staged copy and the route's spill share one root and one temp-disk budget."""
    return protected_scratch_dir(parent, prefix="decoy-ooc-run-")


def open_private(path: Path) -> BinaryIO:
    """A writable file object over ``path``, created or truncated with mode 0600 on its own
    descriptor before any byte is written.

    Scratch files hold source values, and an inherited umask (0000 makes a path-opened writer's
    file 0666; 0777 makes it 0000) must neither widen nor zero their mode, so the mode is set on
    the descriptor rather than left to ``open(2)``'s umask-masked creation mode. The caller owns
    the returned object and closes it; a pyarrow writer handed it never does.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, "wb")
    except BaseException:
        os.close(fd)
        raise


def close_private(writer: object | None, sink: BinaryIO | None) -> None:
    """Close a pyarrow writer and then the ``open_private`` sink under it (pyarrow never closes
    a file object it was handed), the sink even when the writer's close raises."""
    try:
        if writer is not None:
            writer.close()  # type: ignore[attr-defined]
    finally:
        if sink is not None:
            sink.close()


def precreate_private(path: Path) -> str:
    """``path``, created or truncated 0600 for a writer that only takes a name (DuckDB ``COPY``
    run with ``USE_TMP_FILE false``, so it writes this inode in place, never a temp file it then
    renames over it with an umask-derived mode)."""
    open_private(path).close()
    return str(path)


def private_dir(path: Path) -> Path:
    """``mkdir -p`` in which every level it creates is 0700, whatever the umask."""
    return open_protected_dir(path, create=True, parents=True)


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

"""Private scratch helpers the out-of-core runner uses beneath its scratch arena, and the subset
stage's per-run root and reader (`engine_scratch_root`, `scratch_access_for`) until that stage
moves onto the arena."""

from __future__ import annotations

import os
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from decoy_engine.fileaccess import ScratchArena, open_protected_dir, protected_scratch_dir

if TYPE_CHECKING:
    from decoy_engine.fileaccess._scratch import ScratchAccess


def engine_scratch_root(parent: Path | None) -> AbstractContextManager[Path]:
    """A private per-run root the engine creates, removed on every exit.

    Under a caller-named `parent` (ambient policy only) or the system temp directory."""
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


def scratch_access_for(directory: Path) -> ScratchAccess:
    """The reader for files the engine wrote under `directory`, a directory it created.

    Transitional, for the Path arm of `stage_typed_sources` (the subset stage under
    `engine_scratch_root`), which goes when that stage moves onto the arena; the reader itself is
    issued only by `ScratchArena.reader`."""
    with ScratchArena._adopt(directory) as adopted:  # non-owning: exit closes only its descriptor
        return adopted.reader()

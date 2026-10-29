"""F25 scratch seam for the out-of-core FK route (T01-T08).

The capability model lives in ``decoy_engine.scratch._seam`` (the one seam every
producer shares); this module re-exports the generic helpers the out-of-core
producers already import from here and adds ``edge_work_dir``, the one allocator
specific to this route's spill-directory layout. Keeping the import path stable
(``decoy_engine.execution.out_of_core._scratch``) means the Slice 2 producers
and the allocation sentry are untouched by the Slice 3 factoring.

See the shared seam's module docstring for the capability contract: registration
precedes the first writable path, the physical name is a generated opaque
component, and a producer reaches its path only through ``unwrap_dir`` /
``unwrap_path``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from decoy_engine.scratch._capabilities import unwrap_dir
from decoy_engine.scratch._policy import ResourceKind
from decoy_engine.scratch._seam import (
    ArenaLike,
    LocalArena,
    adopt_file,
    as_native_dir,
    ensure_scratch,
    native_dir_path,
)

if TYPE_CHECKING:
    from decoy_engine.scratch._capabilities import ArenaCapability


def edge_work_dir(arena: ArenaLike | None, temp_dir: Path, *, writer_id: str) -> Path:
    """One registered work directory for a producer's spill: allocated from the
    threaded attempt arena, or the adopted ``temp_dir`` on the pre-activation
    path. The producer keeps its original named children under it, so the layout
    (and the output) is unchanged while every write stays capability-anchored."""

    if arena is not None:
        return unwrap_dir(arena.allocate_native_dir(ResourceKind.NATIVE_DIR, writer_id))
    return native_dir_path(temp_dir, writer_id=writer_id)


__all__ = [
    "ArenaCapability",
    "ArenaLike",
    "LocalArena",
    "adopt_file",
    "as_native_dir",
    "edge_work_dir",
    "ensure_scratch",
    "native_dir_path",
]

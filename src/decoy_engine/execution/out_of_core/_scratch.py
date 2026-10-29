"""F25 Slice 2 scratch seam for the out-of-core FK route.

The plan's runtime choke-point is a typed capability: a temporary producer may
obtain a writable path or FD only by holding a ``NativeDirectory`` /
``RegisteredFile`` the manager issued after durable registration (plan 2). This
module threads that capability into every T01-T08 producer.

Two allocators implement the same capability surface:

* ``decoy_engine.scratch.Arena`` (the Slice 1 subsystem, SQLite ledger + kernel
  project quota) is used when a qualified attempt arena is injected through the
  execution context. That is the production-activated path and the one the GCP
  real-quota A02/A03 suite exercises.
* ``LocalArena`` here is the pre-activation / dev / unit-test allocator: it
  creates the same owned 0700 opaque dirs and 0600 files and hands out the same
  capability objects, but without a durable ledger or kernel quota. It is NOT a
  qualified F25 backend; Slice 4/6 activation refuses it and requires an
  injected qualified arena (plan Slice 4 gate: Slices 1-3 are internal migration
  increments, never shipped as completed F25; no managed-plaintext production).

Either way, registration precedes the first writable path, the physical name is
a generated opaque component (never a table/tenant string, plan 1.5), and the
producer reaches its path only through ``unwrap_dir`` / ``unwrap_path``. That is
what lets the allocation sentry enforce T01-T08: every data write is anchored to
a capability and the syscall trace finds it under a registered arena root.
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from decoy_engine.scratch._capabilities import (
    NativeDirectory,
    RegisteredFile,
    unwrap_dir,
)
from decoy_engine.scratch._policy import ResourceKind

if TYPE_CHECKING:
    from decoy_engine.scratch._capabilities import ArenaCapability


@runtime_checkable
class ArenaLike(Protocol):
    """The capability-issuing surface the OOC producers depend on.

    Both ``decoy_engine.scratch.Arena`` and ``LocalArena`` satisfy it, so a
    producer is written against the capability seam, never against either
    concrete backend.
    """

    def allocate_native_dir(self, kind: ResourceKind, writer_id: str) -> NativeDirectory: ...

    def allocate_file(self, kind: ResourceKind, opaque_name: str) -> RegisteredFile: ...


class LocalArena:
    """Dev / unit-test capability allocator (no durable ledger, no kernel quota).

    Creates one owned, opaque, 0700 arena root beneath ``stage_root`` and hands
    out opaque children under it. The ``opaque_name`` / ``writer_id`` a producer
    passes is kept only as a label, never used as a filesystem component (plan
    1.5), so a table or tenant string can never become path traversal even on
    this unmanaged path.
    """

    def __init__(self, stage_root: Path) -> None:
        stage_root.mkdir(parents=True, exist_ok=True)
        self._root = stage_root / f".decoy-arena-{uuid.uuid4().hex}"
        self._root.mkdir(mode=0o700)
        os.chmod(self._root, 0o700)
        self._arena_id = self._root.name
        self._paths: dict[str, Path] = {}

    @property
    def root(self) -> Path:
        return self._root

    def _child(self) -> tuple[str, Path]:
        resource_id = uuid.uuid4().hex
        return resource_id, self._root / resource_id

    def allocate_native_dir(self, kind: ResourceKind, writer_id: str) -> NativeDirectory:
        resource_id, path = self._child()
        path.mkdir(mode=0o700)
        self._paths[resource_id] = path
        return NativeDirectory(
            arena_id=self._arena_id,
            resource_id=resource_id,
            writer_id=writer_id,
            _native_root=path,
        )

    def allocate_file(self, kind: ResourceKind, opaque_name: str) -> RegisteredFile:
        resource_id, path = self._child()
        # O_EXCL so we never adopt a planted file; 0600 from creation (plan A01).
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        self._paths[resource_id] = path
        return RegisteredFile(
            arena_id=self._arena_id,
            resource_id=resource_id,
            kind=kind,
            _writable_path=path,
        )

    def release_all(self) -> None:
        shutil.rmtree(self._root, ignore_errors=True)
        self._paths.clear()


def ensure_scratch(arena: ArenaLike | None, stage_root: Path) -> ArenaLike:
    """Return the injected attempt arena, or a fresh ``LocalArena`` under
    ``stage_root`` for the pre-activation / direct-call path."""

    if arena is not None:
        return arena
    return LocalArena(stage_root)


def as_native_dir(source: NativeDirectory | Path, *, writer_id: str) -> NativeDirectory:
    """Resolve a work directory to a ``NativeDirectory`` capability.

    Already a capability -> returned unchanged (the production path: the arena
    issued it after durable registration). A raw ``Path`` -> that exact owned
    0700 directory is adopted as the native root, so a direct unit-test caller
    (and any legacy sub-path a producer still builds by name) keeps writing its
    sibling files in the same place while the write is still reached through the
    ``unwrap_dir`` capability seam. Adopting a caller-fresh work directory is the
    pre-activation path only; Slice 4/6 require an injected arena capability.
    """

    if isinstance(source, NativeDirectory):
        return source
    source.mkdir(parents=True, exist_ok=True)
    os.chmod(source, 0o700)
    return NativeDirectory(
        arena_id="adopted-path",
        resource_id=uuid.uuid4().hex,
        writer_id=writer_id,
        _native_root=source,
    )


def adopt_file(
    source: RegisteredFile | Path, *, kind: ResourceKind = ResourceKind.FILE
) -> RegisteredFile:
    """Resolve a single spill file to a ``RegisteredFile`` capability.

    Already a capability -> returned unchanged (the arena issued it). A raw
    ``Path`` -> that exact path is adopted as a registered file so a file writer
    (``SpillChildKeys``, ``SpillPayloadStore``, ...) keeps its original name
    under a work directory the producer already allocated from the arena, while
    its write is still reached through ``unwrap_path``. The producer owns the
    work directory's registration; this only names a file inside it.
    """

    if isinstance(source, RegisteredFile):
        return source
    return RegisteredFile(
        arena_id="adopted-path",
        resource_id=uuid.uuid4().hex,
        kind=kind,
        _writable_path=source,
    )


def native_dir_path(source: NativeDirectory | Path, *, writer_id: str) -> Path:
    """The writable directory path for a ``NativeDirectory | Path`` work dir,
    always reached through the ``unwrap_dir`` capability seam."""

    return unwrap_dir(as_native_dir(source, writer_id=writer_id))


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

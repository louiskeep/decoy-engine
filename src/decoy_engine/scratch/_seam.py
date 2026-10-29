"""Shared capability seam for every protected-scratch producer (plan 2).

The plan's runtime choke-point is a typed capability: a temporary producer may
obtain a writable path or FD only by holding a ``NativeDirectory`` /
``RegisteredFile`` / ``PublicationStage`` the manager issued after durable
registration. Slice 2 threaded that capability into the out-of-core FK route
(T01-T08); this module is the same seam, factored so producers *outside* the
out-of-core package (the transactional/hold-back sinks, isolated run, the
PostgreSQL snapshot, subset, quarantine, fixed-width and discovery: T09-T17)
reach their path through one set of helpers instead of a raw ``tempfile`` call.

Two allocators implement the one capability surface:

* ``decoy_engine.scratch.Arena`` (the Slice 1 subsystem: SQLite ledger + kernel
  project quota) is used when a qualified attempt arena is injected. That is the
  production-activated path; registration is durable before any writable path is
  returned, and (Slice 4) encryption is inherited by every child name.
* ``LocalArena`` here is the pre-activation / dev / unit-test allocator: it
  creates the same owned 0700 opaque dirs and 0600 files and hands out the same
  capability objects, but with no durable ledger and no kernel quota. It is NOT
  a qualified F25 backend; activation (Slice 4/6) refuses it and requires an
  injected qualified arena (no managed-plaintext production).

Either way the physical name is a generated opaque component (never a table,
tenant or schema string, plan 1.5), registration precedes the first writable
path, and the producer reaches its path only through ``unwrap_dir`` /
``unwrap_path``. The ``ensure_*`` helpers below add the one decision every
non-OOC producer shares: allocate a *registered* resource from an injected
arena, or adopt an owned raw ``tempfile`` directory/file on the pre-activation
path while still routing the write through the capability seam. The raw
``tempfile`` call lives here, in the seam, never in a migrated producer, so the
allocation sentry can keep every producer module free of unregistered
allocation.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, Protocol, runtime_checkable

from decoy_engine.scratch._capabilities import (
    NativeDirectory,
    PublicationStage,
    RegisteredFile,
    unwrap_dir,
    unwrap_path,
)
from decoy_engine.scratch._policy import ResourceKind

if TYPE_CHECKING:
    from decoy_engine.scratch._capabilities import ArenaCapability

# The sentinel arena_id an *adopted* (pre-activation) capability carries: it was
# not issued by a durable-ledger arena, so EDQUOT routing and release fall back
# to the producer's own handling rather than the ledger (plan Slice 4/6: an
# adopted path is never a qualified backend).
_ADOPTED = "adopted-path"


@runtime_checkable
class ArenaLike(Protocol):
    """The capability-issuing surface a producer depends on.

    Both ``decoy_engine.scratch.Arena`` and ``LocalArena`` satisfy it, so a
    producer is written against the capability seam, never against either
    concrete backend. ``allocate_publication`` and ``report_write_failure`` are
    only on the real ``Arena``; the helpers below probe for them with
    ``hasattr`` so ``LocalArena`` and the adopted path degrade cleanly.
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
        arena_id=_ADOPTED,
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
    keeps its original name under a work directory the producer already
    allocated from the arena, while its write is still reached through
    ``unwrap_path``.
    """

    if isinstance(source, RegisteredFile):
        return source
    return RegisteredFile(
        arena_id=_ADOPTED,
        resource_id=uuid.uuid4().hex,
        kind=kind,
        _writable_path=source,
    )


def native_dir_path(source: NativeDirectory | Path, *, writer_id: str) -> Path:
    """The writable directory path for a ``NativeDirectory | Path`` work dir,
    always reached through the ``unwrap_dir`` capability seam."""

    return unwrap_dir(as_native_dir(source, writer_id=writer_id))


def ensure_native_dir(
    arena: ArenaLike | None,
    *,
    fallback_parent: Path | None = None,
    writer_id: str,
    kind: ResourceKind = ResourceKind.NATIVE_DIR,
    fallback_prefix: str = ".decoy-native-",
) -> tuple[Path, NativeDirectory | None]:
    """One registered opaque directory for a native library's spill.

    With an injected arena the directory is allocated (registered before use)
    and the capability is returned so the caller can release it and route a
    write failure through it. On the pre-activation path an owned 0700 directory
    is created under ``fallback_parent`` with ``fallback_prefix`` and adopted
    through the seam; ``fallback_parent`` None uses the system temp root (the
    default ``tempfile.mkdtemp`` location), matching the pre-migration behavior.
    Its lifetime is the caller's, as before. Returns ``(path, capability)``;
    ``capability`` is ``None`` on the adopted path.
    """

    if arena is not None:
        cap = arena.allocate_native_dir(kind, writer_id)
        return unwrap_dir(cap), cap
    if fallback_parent is not None:
        fallback_parent.mkdir(parents=True, exist_ok=True)
    raw = Path(tempfile.mkdtemp(prefix=fallback_prefix, dir=fallback_parent))
    return native_dir_path(raw, writer_id=writer_id), None


def ensure_publication_stage(
    arena: ArenaLike | None,
    destination: Path,
    *,
    retention_owner: str,
    fallback_prefix: str,
) -> tuple[Path, PublicationStage | None]:
    """A destination-side staging directory that becomes durable output on the
    existing single rename (plan 1.6, T09/T12/T13/T14/T15/T16).

    With an injected arena ``allocate_publication`` registers a stage beside the
    destination (same filesystem verified, cross-device refused before any data)
    and the ``PublicationStage`` is returned. On the pre-activation path an owned
    directory is created beside ``destination`` with ``fallback_prefix`` (the
    producer's existing staging name, so orphan-identification by prefix is
    preserved) and adopted. Returns ``(stage_path, capability)``.
    """

    allocate_publication = getattr(arena, "allocate_publication", None)
    if allocate_publication is not None:
        cap: PublicationStage = allocate_publication(destination, retention_owner)
        return unwrap_path(cap), cap
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=fallback_prefix, dir=parent))
    return stage, None


def ensure_publication_file(
    arena: ArenaLike | None,
    destination: Path,
    *,
    retention_owner: str,
    fallback_dir: Path,
    fallback_prefix: str,
    fallback_suffix: str = "",
) -> tuple[Path, PublicationStage | None]:
    """A single staged file on the *destination* filesystem, to be published by
    the producer's existing same-filesystem rename (plan 1.6, T15/T16).

    With an injected arena a publication stage is registered beside
    ``destination`` and an opaque 0600 file is created inside it, so the later
    ``os.replace`` onto ``destination`` is still a same-filesystem rename. On the
    pre-activation path ``tempfile.mkstemp`` creates the owned 0600 file directly
    under ``fallback_dir`` with the producer's existing prefix/suffix, exactly as
    before migration. Returns ``(file_path, stage_capability)``; the capability's
    release reclaims the (now empty) stage after the rename.
    """

    allocate_publication = getattr(arena, "allocate_publication", None)
    if allocate_publication is not None:
        cap: PublicationStage = allocate_publication(destination, retention_owner)
        stage = unwrap_path(cap)
        fd, name = tempfile.mkstemp(prefix=fallback_prefix, suffix=fallback_suffix, dir=stage)
        os.close(fd)
        return Path(name), cap
    fallback_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=fallback_prefix, suffix=fallback_suffix, dir=fallback_dir)
    os.close(fd)
    return Path(name), None


def ensure_scratch_file(
    arena: ArenaLike | None,
    *,
    fallback_dir: Path,
    fallback_prefix: str,
    fallback_suffix: str = "",
    kind: ResourceKind = ResourceKind.FILE,
) -> tuple[Path, RegisteredFile | None]:
    """A single registered file for a producer's staged write.

    With an injected arena the file is allocated (registered, 0600, O_EXCL)
    under the arena root and its capability returned. On the pre-activation path
    ``tempfile.mkstemp`` creates an owned 0600 file under ``fallback_dir`` with
    the producer's existing prefix/suffix (so its staging name is preserved) and
    adopts it. Returns ``(path, capability)``.
    """

    if arena is not None:
        cap = arena.allocate_file(kind, f"{fallback_prefix}{uuid.uuid4().hex}{fallback_suffix}")
        return unwrap_path(cap), cap
    fallback_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=fallback_prefix, suffix=fallback_suffix, dir=fallback_dir)
    os.close(fd)
    return Path(name), None


def release_resource(
    arena: ArenaLike | None,
    cap: RegisteredFile | NativeDirectory | PublicationStage | None,
) -> None:
    """Best-effort ledger release of one registered resource.

    Only a capability a real ``Arena`` issued is released through the ledger; an
    adopted (pre-activation) capability or a missing arena is left to the
    producer's own cleanup, exactly as before migration.
    """

    release = getattr(arena, "release", None)
    if release is None or cap is None or getattr(cap, "arena_id", _ADOPTED) == _ADOPTED:
        return
    try:
        release(cap)
    except Exception:
        # Release accounting failures are the sweep's problem (plan 1.8); they
        # must never mask the producer's own success/failure path.
        pass


def route_write_failure(
    arena: ArenaLike | None,
    cap: RegisteredFile | NativeDirectory | PublicationStage | None,
    exc: OSError,
) -> NoReturn:
    """Turn a pyarrow/duckdb/native write ``OSError`` into the shared typed
    quota failure, or re-raise it unchanged (A02).

    EDQUOT/ENOSPC are classified by ``errno`` only, through
    ``Arena.report_write_failure`` (never by parsing a native exception string,
    plan 1.5 item 5). When the arena is the pre-activation allocator or the
    resource was adopted, there is no ledger to report against, so the original
    exception propagates unchanged (its errno still lets a public wrapper
    classify it). This helper always raises.
    """

    reporter = getattr(arena, "report_write_failure", None)
    if reporter is not None and cap is not None and getattr(cap, "arena_id", _ADOPTED) != _ADOPTED:
        reporter(cap, exc)  # raises ScratchQuotaExceeded, or re-raises exc for a non-quota errno
    raise exc


__all__ = [
    "ArenaCapability",
    "ArenaLike",
    "LocalArena",
    "adopt_file",
    "as_native_dir",
    "ensure_native_dir",
    "ensure_publication_file",
    "ensure_publication_stage",
    "ensure_scratch",
    "ensure_scratch_file",
    "native_dir_path",
    "release_resource",
    "route_write_failure",
]

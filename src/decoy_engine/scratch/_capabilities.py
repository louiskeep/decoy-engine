"""Typed capabilities that authorize a protected-scratch write.

The runtime choke-point is: a producer may obtain a writable path or FD only by
holding one of these capabilities, which the ScratchManager issues *after*
durable registration, root creation, quota verification and lease validation
(plan 2). A bare ``pathlib.Path`` authorizes nothing. Only storage adapters
unwrap a capability to the underlying path, through ``unwrap_path`` /
``unwrap_dir`` / ``open_capability_fd`` here, which is the one sanctioned seam
the allocation sentry knows about.

Identity-bound handles (argon 2026-10-30 plan-remediation, after the 3-round
publication-TOCTOU cap). A capability is not a wrapper around a ``pathlib.Path``;
it carries the kernel object identity of its target and of its parent, pinned at
allocation: ``_parent_identity`` / ``_identity`` are ``(st_dev, st_ino)`` tuples
and ``_expected_mode`` is the full ``S_IFMT | perm`` the target must still have.
``unwrap_path`` / ``unwrap_dir`` / ``open_capability_fd`` re-verify that identity
with ``O_NOFOLLOW`` at the moment of use and fail closed with ``ScratchPathUnsafe``
on any post-allocation parent-or-child substitution. Capabilities stay frozen
value objects with no long-lived file descriptors: a descriptor is opened,
``fstat``-verified against the immutable identity, used for the ``*at`` syscall
and closed in a ``finally`` within the same call, so a capability is safe to pass
across threads or outlive its creating call without leaking FDs.

An *adopted* / pre-activation capability (the dev ``LocalArena`` and the
``_seam`` fallbacks) carries ``None`` identity: it was not issued by a qualified
durable-ledger arena, so there is nothing pinned to re-verify and the unwrap seam
returns the stored path unchanged, exactly as before this change. Use-time
identity verification is the production ``Arena`` path's guarantee; the
pre-activation allocator is explicitly not a qualified F25 backend (plan Slice
4/6) and keeps its prior behavior.

``ArenaCapability`` carries the attempt identity, fence generation, the approved
root and the quota assignment; it is threaded through the execution context into
each low-level producer (plan 1.5). It does not itself expose a writable path:
the per-resource capabilities do, and only via the unwrap functions.
"""

from __future__ import annotations

import os
import stat as _stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from decoy_engine.scratch._errors import ScratchPathUnsafe
from decoy_engine.scratch._policy import LeaseIdentity, ResourceKind

# Kernel object identity: (st_dev, st_ino). ``None`` on an adopted/pre-activation
# capability that was not issued by a qualified durable-ledger arena.
Identity = tuple[int, int]

# The full mode (type + permission bits) a target must still carry at use time.
FILE_MODE = _stat.S_IFREG | 0o600
DIR_MODE = _stat.S_IFDIR | 0o700


@dataclass(frozen=True, slots=True)
class ArenaCapability:
    """The attempt-scoped capability threaded through execution.

    A subprocess child receives this plus the liveness-lock FD only over a
    restricted inherited channel, never through argv/env/path (plan 1.5 A04).
    Slice 1 defines the shape; the restricted-fd transport is Slice 3/4.
    """

    arena_id: str
    lease: LeaseIdentity
    quota_bytes: int
    quota_inodes: int
    # The verified arena root. Stored for the manager/provider to resolve
    # children beneath; holding this path is not itself write authority.
    approved_root: Path


@dataclass(frozen=True, slots=True)
class RegisteredFile:
    """A single registered Python-created file. The write target is reachable
    only via ``unwrap_path`` / ``open_capability_fd``, which re-verify the pinned
    ``(st_dev, st_ino)`` identity at use time (plan 2: only storage adapters
    unwrap)."""

    arena_id: str
    resource_id: str
    kind: ResourceKind
    _writable_path: Path
    _parent_identity: Identity | None = None
    _identity: Identity | None = None
    _expected_mode: int = FILE_MODE


@dataclass(frozen=True, slots=True)
class NativeDirectory:
    """A registered opaque directory handed to a native library. Its whole
    inherited namespace is covered by one directory-level record (plan 1.5);
    inner filenames are the library's to choose and inherit the quota and (from
    Slice 4) the encryption domain automatically."""

    arena_id: str
    resource_id: str
    writer_id: str
    _native_root: Path
    _parent_identity: Identity | None = None
    _identity: Identity | None = None
    _expected_mode: int = DIR_MODE


@dataclass(frozen=True, slots=True)
class PublicationStage:
    """A registered destination-side staging area. Its bytes are counted until a
    durable ownership transfer; the single rename that publishes it is the
    existing publisher's (plan 1.6). Key lifetime is the destination's, not the
    ephemeral arena's."""

    arena_id: str
    resource_id: str
    destination: Path
    retention_owner: str
    _stage_path: Path
    _parent_identity: Identity | None = None
    _identity: Identity | None = None
    _expected_mode: int = DIR_MODE


@dataclass(frozen=True, slots=True)
class CheckpointRef:
    """A handle to a resumable checkpoint generation. Validity is a committed
    ledger state (``CHECKPOINT_VALID``), never a filename convention (plan 1.7).
    The recovery envelope and ``verify()`` are Slice 4/6; Slice 1 is the handle
    and the state transition only."""

    arena_id: str
    resource_id: str
    generation: int
    _payload_path: Path
    _parent_identity: Identity | None = None
    _identity: Identity | None = None
    _expected_mode: int = DIR_MODE


_Capability = RegisteredFile | NativeDirectory | PublicationStage | CheckpointRef


def _target_path(capability: _Capability) -> Path:
    if isinstance(capability, RegisteredFile):
        return capability._writable_path
    if isinstance(capability, NativeDirectory):
        return capability._native_root
    if isinstance(capability, PublicationStage):
        return capability._stage_path
    return capability._payload_path


def _reject_unsafe_name(name: str) -> None:
    if "/" in name or name in ("", ".", ".."):
        raise ScratchPathUnsafe(message="capability target is not a single safe path component")


@dataclass(frozen=True, slots=True)
class VerifiedDir:
    """An open ``O_RDONLY|O_NOFOLLOW|O_DIRECTORY`` descriptor whose
    ``(st_dev, st_ino)`` has been ``fstat``-verified. The operation runs on
    ``fd`` (and ``*at`` calls anchored to it); ``path`` is diagnostic only and is
    never passed to an ``os.*`` call."""

    fd: int
    identity: Identity
    path: Path


@dataclass(frozen=True, slots=True)
class VerifiedChild:
    """An open, verified parent ``dir_fd`` plus the single-component child name
    and the child's pinned identity/mode. The operation acts on ``name`` only via
    ``*at(..., dir_fd=parent_fd)`` calls; the parent fd stays open for the life of
    the surrounding context."""

    parent_fd: int
    name: str
    parent_identity: Identity
    identity: Identity
    expected_mode: int


@contextmanager
def open_verified_dir(
    path: Path, expected_identity: Identity | None = None, *, require_owner: bool = True
) -> Iterator[VerifiedDir]:
    """Open ``path`` as a directory ``O_RDONLY|O_NOFOLLOW|O_DIRECTORY`` and yield
    an ``fstat``-verified handle, so the caller runs every subsequent syscall on
    the open ``fd`` and never re-resolves the path string. A symlink at the final
    component trips ``O_NOFOLLOW``; a changed ``(st_dev, st_ino)`` (when an
    expected identity is pinned) or wrong owner fails closed with
    ``ScratchPathUnsafe``. ``require_owner`` is relaxed for a caller-owned
    publication destination parent, where the pinned identity, not ownership, is
    the guard."""

    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    except OSError:
        raise ScratchPathUnsafe(message="directory failed use-time verification") from None
    try:
        st = os.fstat(fd)
        ident = (st.st_dev, st.st_ino)
        if expected_identity is not None and ident != expected_identity:
            raise ScratchPathUnsafe(message="directory identity changed since pinning")
        if require_owner and st.st_uid != os.geteuid():
            raise ScratchPathUnsafe(message="directory is not owned by this principal")
        yield VerifiedDir(fd=fd, identity=ident, path=path)
    finally:
        os.close(fd)


@contextmanager
def open_verified_target(
    path: Path,
    parent_identity: Identity,
    identity: Identity,
    expected_mode: int,
) -> Iterator[VerifiedChild]:
    """Open and verify a capability target, yielding a ``VerifiedChild`` whose
    parent ``dir_fd`` stays open for the caller's ``*at`` operation (reclaim,
    stat, delete). The child is resolved relative to the ``fstat``-verified
    parent fd ``O_NOFOLLOW``, its ``(st_dev, st_ino)`` from both ``fstat`` and a
    ``dir_fd``-relative ``lstat`` must equal the pinned identity, and its owner
    and mode must be unchanged. Any mismatch or ``OSError`` fails closed with
    ``ScratchPathUnsafe``. The parent is not required to be owned by this
    principal (a publication destination is caller-owned); the pinned identity is
    the guard."""

    _reject_unsafe_name(path.name)
    expect_dir = _stat.S_ISDIR(expected_mode)
    with open_verified_dir(path.parent, parent_identity, require_owner=False) as parent:
        try:
            lst = os.lstat(path.name, dir_fd=parent.fd)
            child_flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if expect_dir else 0)
            child_fd = os.open(path.name, child_flags, dir_fd=parent.fd)
            try:
                cst = os.fstat(child_fd)
            finally:
                os.close(child_fd)
        except OSError:
            raise ScratchPathUnsafe(
                message="capability target failed use-time verification"
            ) from None
        if (cst.st_dev, cst.st_ino) != identity or (lst.st_dev, lst.st_ino) != identity:
            raise ScratchPathUnsafe(message="capability target identity changed since allocation")
        if cst.st_uid != os.geteuid():
            raise ScratchPathUnsafe(message="capability target is not owned by this principal")
        if cst.st_mode != expected_mode:
            raise ScratchPathUnsafe(message="capability target type or mode changed")
        yield VerifiedChild(
            parent_fd=parent.fd,
            name=path.name,
            parent_identity=parent_identity,
            identity=identity,
            expected_mode=expected_mode,
        )


def _verify_capability_at_use(capability: _Capability) -> None:
    """Re-verify a capability target at use time by entering and immediately
    leaving ``open_verified_target``. Used only by ``unwrap_path`` / ``unwrap_dir``
    for native-library callers that need a ``char*`` path: those re-resolve the
    returned string, so a narrow residual window remains (documented in the plan
    threat model). A Python writer uses ``open_capability_fd`` instead, which acts
    on the open descriptor with no re-resolution."""

    if capability._identity is None or capability._parent_identity is None:
        return
    with open_verified_target(
        _target_path(capability),
        capability._parent_identity,
        capability._identity,
        capability._expected_mode,
    ):
        pass


def unwrap_path(capability: RegisteredFile | PublicationStage) -> Path:
    """Return the writable path a file/publication capability authorizes, after
    re-verifying its pinned identity at use time.

    This is the sanctioned unwrap seam. A producer that reaches for a raw path
    instead of calling this (or ``unwrap_dir``) is what the allocation sentry is
    built to catch (plan 2 check 1). The returned ``Path`` is still a string the
    caller re-resolves, so a residual window remains for native libraries that
    take only a ``char*``; a Python writer should prefer ``open_capability_fd``,
    which hands back an ``fstat``-verified descriptor and does FD-relative I/O
    with no path re-resolution after the check.
    """

    _verify_capability_at_use(capability)
    if isinstance(capability, RegisteredFile):
        return capability._writable_path
    return capability._stage_path


def unwrap_dir(capability: NativeDirectory) -> Path:
    """Return the native-library-writable directory a capability authorizes,
    after re-verifying its pinned identity at use time."""

    _verify_capability_at_use(capability)
    return capability._native_root


def open_capability_fd(
    capability: RegisteredFile | NativeDirectory | PublicationStage,
    *,
    flags: int = os.O_WRONLY,
) -> int:
    """Open a capability's verified target relative to its pinned parent and
    return an ``fstat``-verified file descriptor (argon use-time seam).

    For a Python writer this closes the residual window ``unwrap_path`` leaves:
    the parent is opened ``O_NOFOLLOW`` and its pinned ``(st_dev, st_ino)``
    confirmed, the child is opened relative to that ``dir_fd`` (so no path string
    is re-resolved by the kernel after the check), and the returned descriptor is
    ``fstat``-confirmed against the pinned child identity and owner before it is
    handed back. A directory target is opened ``O_RDONLY|O_DIRECTORY`` regardless
    of ``flags``. The caller owns the returned FD and must close it. An adopted
    capability (no pinned identity) is opened ``O_NOFOLLOW`` without an identity
    assertion, matching its unverified pre-activation contract. Any mismatch or
    ``OSError`` fails closed with ``ScratchPathUnsafe``.
    """

    path = _target_path(capability)
    parent_identity = capability._parent_identity
    identity = capability._identity
    expect_dir = _stat.S_ISDIR(capability._expected_mode)
    _reject_unsafe_name(path.name)
    parent_fd = -1
    fd = -1
    try:
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
        if parent_identity is not None:
            pst = os.fstat(parent_fd)
            if (pst.st_dev, pst.st_ino) != parent_identity:
                raise ScratchPathUnsafe(
                    message="capability parent identity changed since allocation"
                )
        open_flags = (
            os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY if expect_dir else flags | os.O_NOFOLLOW
        )
        fd = os.open(path.name, open_flags, dir_fd=parent_fd)
        cst = os.fstat(fd)
        if identity is not None:
            if (cst.st_dev, cst.st_ino) != identity:
                raise ScratchPathUnsafe(
                    message="capability target identity changed since allocation"
                )
            if cst.st_mode != capability._expected_mode:
                raise ScratchPathUnsafe(message="capability target type or mode changed")
            if cst.st_uid != os.geteuid():
                raise ScratchPathUnsafe(message="capability target is not owned by this principal")
    except ScratchPathUnsafe:
        if fd >= 0:
            os.close(fd)
        raise
    except OSError:
        if fd >= 0:
            os.close(fd)
        raise ScratchPathUnsafe(message="capability target could not be opened for use") from None
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)
    return fd

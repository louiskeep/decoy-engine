"""Typed capabilities that authorize a protected-scratch write.

The runtime choke-point is: a producer may obtain a writable path or FD only by
holding one of these capabilities, which the ScratchManager issues *after*
durable registration, root creation, quota verification and lease validation
(plan 2). A bare ``pathlib.Path`` authorizes nothing. Only storage adapters
unwrap a capability to the underlying path, through ``unwrap_path`` /
``unwrap_dir`` here, which is the one sanctioned seam the allocation sentry
knows about.

``ArenaCapability`` carries the attempt identity, fence generation, the approved
root and the quota assignment; it is threaded through the execution context into
each low-level producer (plan 1.5). It does not itself expose a writable path:
the per-resource capabilities do, and only via the unwrap functions.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from decoy_engine.scratch._policy import LeaseIdentity, ResourceKind


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
    only via ``unwrap_path`` (plan 2: only storage adapters unwrap)."""

    arena_id: str
    resource_id: str
    kind: ResourceKind
    _writable_path: Path


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


def unwrap_path(capability: RegisteredFile | PublicationStage) -> Path:
    """Return the writable path a file/publication capability authorizes.

    This is the sanctioned unwrap seam. A producer that reaches for a raw path
    instead of calling this (or ``unwrap_dir``) is what the allocation sentry is
    built to catch (plan 2 check 1).
    """

    if isinstance(capability, RegisteredFile):
        return capability._writable_path
    return capability._stage_path


def unwrap_dir(capability: NativeDirectory) -> Path:
    """Return the native-library-writable directory a capability authorizes."""

    return capability._native_root

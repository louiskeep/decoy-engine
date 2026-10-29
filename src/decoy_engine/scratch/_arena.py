"""The attempt-scoped arena: the one object that hands out writable capabilities.

An ``Arena`` is issued by ``ScratchManager.open_attempt`` after the ownership
authority, qualified backend, control ledger and data arena are all in place.
Every allocation here registers a durable ``CREATING`` row *before* it creates a
writable path or FD, so registration always precedes the first write (plan 1.5,
A01). The physical filename is a generated opaque component; the caller's
``opaque_name`` is kept only as a ledger label, never used as a path, so a
table/tenant string can never become filesystem traversal (plan 1.5).

Checkpoint and publication carry their full API surface here; the recovery
envelope, encryption and the destructive sweep are later slices (4/5). Slice 1
builds the registration, capability issuance, quota-failure reporting and
cleanup accounting.
"""

from __future__ import annotations

import errno as _errno
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from decoy_engine.scratch._capabilities import (
    ArenaCapability,
    CheckpointRef,
    NativeDirectory,
    PublicationStage,
    RegisteredFile,
)
from decoy_engine.scratch._errors import (
    ScratchCleanupError,
    ScratchPathUnsafe,
    ScratchQuotaExceeded,
)
from decoy_engine.scratch._ledger import ResourceLedger
from decoy_engine.scratch._policy import (
    LeaseIdentity,
    ResourceKind,
    ResourceState,
    Retention,
    ScratchPolicy,
)
from decoy_engine.scratch._provider import StorageProvider
from decoy_engine.scratch._reports import (
    CleanupReport,
    Disposition,
    QuotaObservation,
    QuotaReport,
)

if TYPE_CHECKING:
    from decoy_engine.scratch._manager import ScratchManager

_QUOTA_ERRNOS = frozenset({_errno.EDQUOT, _errno.ENOSPC})


class Arena:
    """A single attempt's registered resource space."""

    def __init__(
        self,
        *,
        manager: ScratchManager,
        ledger: ResourceLedger,
        provider: StorageProvider,
        policy: ScratchPolicy,
        lease: LeaseIdentity,
        arena_id: str,
        root: Path,
        quota_domain_id: str,
        owner_principal: str,
    ) -> None:
        self._manager = manager
        self._ledger = ledger
        self._provider = provider
        self._policy = policy
        self._lease = lease
        self._arena_id = arena_id
        self._root = root
        self._quota_domain_id = quota_domain_id
        self._owner = owner_principal
        self._paths: dict[str, Path] = {}

    @property
    def arena_id(self) -> str:
        return self._arena_id

    @property
    def capability(self) -> ArenaCapability:
        return ArenaCapability(
            arena_id=self._arena_id,
            lease=self._lease,
            quota_bytes=self._policy.quota_bytes,
            quota_inodes=self._policy.quota_inodes,
            approved_root=self._root,
        )

    def _new_resource_id(self) -> str:
        return uuid.uuid4().hex

    def _register(self, resource_id: str, kind: ResourceKind, retention: Retention) -> None:
        root_stat = os.stat(self._root)
        self._ledger.register_resource(
            resource_id=resource_id,
            arena_id=self._arena_id,
            lease=self._lease,
            kind=kind,
            retention=retention,
            owner_principal=self._owner,
            root_dev=root_stat.st_dev,
            root_ino=root_stat.st_ino,
            quota_domain_id=self._quota_domain_id,
            recovery_class=retention.value,
        )

    def _safe_child(self, name: str) -> Path:
        """Resolve a generated component beneath the arena root, rejecting any
        escape or symlink (plan 1.5)."""

        if "/" in name or name in ("", ".", ".."):
            raise ScratchPathUnsafe(message="resource component is unsafe")
        child = self._root / name
        resolved = child.resolve()
        if resolved.parent != self._root.resolve():
            raise ScratchPathUnsafe(message="resource path escapes the arena root")
        return child

    def allocate_file(self, kind: ResourceKind, opaque_name: str) -> RegisteredFile:
        resource_id = self._new_resource_id()
        self._register(resource_id, kind, self._policy.retention)
        path = self._safe_child(resource_id)
        # 0600 metadata/data from creation, O_EXCL so we never adopt a planted file.
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        self._paths[resource_id] = path
        self._ledger.transition(
            resource_id,
            frm=ResourceState.CREATING,
            to=ResourceState.ACTIVE,
            generation=self._lease.generation,
        )
        return RegisteredFile(
            arena_id=self._arena_id, resource_id=resource_id, kind=kind, _writable_path=path
        )

    def allocate_native_dir(self, kind: ResourceKind, writer_id: str) -> NativeDirectory:
        resource_id = self._new_resource_id()
        self._register(resource_id, ResourceKind.NATIVE_DIR, self._policy.retention)
        path = self._safe_child(resource_id)
        os.mkdir(path, mode=0o700)
        self._paths[resource_id] = path
        self._ledger.transition(
            resource_id,
            frm=ResourceState.CREATING,
            to=ResourceState.ACTIVE,
            generation=self._lease.generation,
        )
        return NativeDirectory(
            arena_id=self._arena_id, resource_id=resource_id, writer_id=writer_id, _native_root=path
        )

    def allocate_publication(self, destination: Path, retention_owner: str) -> PublicationStage:
        """Allocate a destination-side staging area. The stage must sit on the
        destination filesystem so the existing single rename can publish it; a
        cross-device destination is refused before any data is staged (plan 1.6,
        A10 verification is Slice 3/4)."""

        resource_id = self._new_resource_id()
        dest_parent = destination.parent if destination.parent.exists() else destination
        stage = dest_parent / f".decoy-stage-{resource_id}"
        self._register(resource_id, ResourceKind.PUBLICATION, Retention.DURABLE_PUBLICATION)
        os.mkdir(stage, mode=0o700)
        if os.stat(stage).st_dev != os.stat(dest_parent).st_dev:
            self._provider.reclaim(stage)
            raise ScratchPathUnsafe(message="publication stage is not on the destination device")
        self._paths[resource_id] = stage
        self._ledger.transition(
            resource_id,
            frm=ResourceState.CREATING,
            to=ResourceState.ACTIVE,
            generation=self._lease.generation,
        )
        return PublicationStage(
            arena_id=self._arena_id,
            resource_id=resource_id,
            destination=destination,
            retention_owner=retention_owner,
            _stage_path=stage,
        )

    def checkpoint(
        self,
        resource: RegisteredFile | NativeDirectory,
        recovery_descriptor: object,
        validator_or_manifest: object,
    ) -> CheckpointRef:
        """Register a resumable checkpoint generation (API surface, Slice 1).

        The ledger transitions ``CHECKPOINT_WRITING`` then ``CHECKPOINT_VALID``;
        the authenticated recovery envelope, the ``verify()`` step and resume are
        Slice 4/6 (plan 1.7). The previous valid generation is kept until this
        transition completes."""

        _ = (recovery_descriptor, validator_or_manifest)
        resource_id = self._new_resource_id()
        self._register(resource_id, ResourceKind.CHECKPOINT, Retention.RESUMABLE)
        payload = self._safe_child(resource_id)
        os.mkdir(payload, mode=0o700)
        self._paths[resource_id] = payload
        self._ledger.transition(
            resource_id,
            frm=ResourceState.CREATING,
            to=ResourceState.CHECKPOINT_WRITING,
            generation=self._lease.generation,
        )
        self._ledger.transition(
            resource_id,
            frm=ResourceState.CHECKPOINT_WRITING,
            to=ResourceState.CHECKPOINT_VALID,
            generation=self._lease.generation,
        )
        return CheckpointRef(
            arena_id=self._arena_id,
            resource_id=resource_id,
            generation=self._lease.generation,
            _payload_path=payload,
        )

    def release(
        self, resource: RegisteredFile | NativeDirectory | PublicationStage | CheckpointRef
    ) -> CleanupReport:
        """Release one resource. Capacity is reported released only after the
        provider confirms reclamation; a failed cleanup keeps its charge and
        inventory and raises ``ScratchCleanupError`` (plan 1.5 item 6)."""

        resource_id = resource.resource_id
        path = self._paths.get(resource_id)
        if path is None or not path.exists():
            self._ledger.set_disposition(
                resource_id, disposition=Disposition.DELETED.value, state=ResourceState.REAPED
            )
            return CleanupReport(
                arena_id=self._arena_id, resource_id=resource_id, disposition=Disposition.DELETED
            )
        confirmed, retained_bytes, retained_inodes = self._provider.reclaim(path)
        if not confirmed:
            self._ledger.set_disposition(
                resource_id,
                disposition=Disposition.RETAINED.value,
                state=ResourceState.FAILED,
                retained_bytes=retained_bytes,
            )
            report = CleanupReport(
                arena_id=self._arena_id,
                resource_id=resource_id,
                disposition=Disposition.RETAINED,
                retained_bytes=retained_bytes,
                retained_inodes=retained_inodes,
                detail="provider could not confirm reclamation",
            )
            raise ScratchCleanupError(report=report)
        self._paths.pop(resource_id, None)
        self._ledger.set_disposition(
            resource_id, disposition=Disposition.DELETED.value, state=ResourceState.REAPED
        )
        return CleanupReport(
            arena_id=self._arena_id, resource_id=resource_id, disposition=Disposition.DELETED
        )

    def report_write_failure(
        self, resource: RegisteredFile | NativeDirectory | PublicationStage, exc: OSError
    ) -> NoReturn:
        """Turn a write/close/fsync ``OSError`` into a typed quota failure.

        EDQUOT/ENOSPC are classified by ``errno`` only, never by parsing a
        native exception string (plan 1.5 item 5). A non-quota errno is
        re-raised unchanged. The attempt stops and publication is prohibited."""

        if exc.errno not in _QUOTA_ERRNOS:
            raise exc
        report = self.build_quota_report(resource, errno=exc.errno)
        self._ledger.set_disposition(
            resource.resource_id,
            disposition=Disposition.RETAINED.value,
            state=ResourceState.FAILED,
        )
        raise ScratchQuotaExceeded(report=report, message="kernel hard quota exceeded")

    def build_quota_report(
        self,
        resource: RegisteredFile | NativeDirectory | PublicationStage,
        *,
        errno: int | None,
    ) -> QuotaReport:
        observation = self._provider.observe(self._root, self._policy)
        dimension = "inodes" if errno == _errno.EDQUOT and _inode_pressure(observation) else "bytes"
        return QuotaReport(
            arena_id=self._arena_id,
            resource_id=resource.resource_id,
            kind=getattr(resource, "kind", ResourceKind.FILE).value
            if hasattr(resource, "kind")
            else "publication",
            effective_bytes_cap=self._policy.quota_bytes,
            effective_inodes_cap=self._policy.quota_inodes,
            observation=observation,
            dimension=dimension,
            disposition=Disposition.RETAINED,
            errno=errno,
        )


def _inode_pressure(observation: QuotaObservation) -> bool:
    used = observation.inodes_used
    return used is not None and used >= observation.inodes_limit

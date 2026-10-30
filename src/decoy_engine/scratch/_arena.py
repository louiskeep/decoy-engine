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
import stat as _stat
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
        destination filesystem so the existing single rename can publish it.

        The stage carries the same path hardening the data arena uses (plan 1.5 /
        1.6), closed against a substitution race: the destination parent is pinned
        with a NO-FOLLOW directory fd (``O_NOFOLLOW|O_DIRECTORY``), so a symlink
        swapped in for the parent after any check cannot redirect the stage to
        another mount. Creation and verification happen RELATIVE to that fd
        (``os.mkdir``/``os.open`` with ``dir_fd=``), never by re-resolving a path
        string, and the stage is verified non-symlink, owner-matched, mode 0700,
        single-device with the pinned parent, and unchanged in device+inode
        identity under the pinned fd. Any failure reclaims the stage and raises
        ``ScratchPathUnsafe``; the data is never staged on an unverified path."""

        resource_id = self._new_resource_id()
        dest_parent = destination.parent if destination.parent.exists() else destination
        stage_name = f".decoy-stage-{resource_id}"
        stage = dest_parent / stage_name
        # Pin the destination parent with a no-follow directory fd. If the parent
        # is itself a symlink the open fails (ELOOP), and every mkdir/stat below
        # is relative to this pinned inode, so a link swapped in after the open
        # cannot redirect the stage off the quota domain (mount-substitution
        # guard, mirroring the data arena's _safe_child discipline, plan 1.5/1.6).
        try:
            dir_fd = os.open(dest_parent, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
        except OSError:
            raise ScratchPathUnsafe(
                message="publication destination parent is not a non-symlink directory"
            ) from None
        try:
            parent_dev = os.fstat(dir_fd).st_dev
            self._register(resource_id, ResourceKind.PUBLICATION, Retention.DURABLE_PUBLICATION)
            os.mkdir(stage_name, mode=0o700, dir_fd=dir_fd)
            self._verify_stage(dir_fd, stage_name, parent_dev)
        finally:
            os.close(dir_fd)
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

    def _verify_stage(self, dir_fd: int, stage_name: str, parent_dev: int) -> None:
        """Verify a freshly created publication stage the same way the data arena
        verifies a child (plan 1.5), all relative to the pinned parent fd:
        non-symlink, owned by this principal, mode 0700, same device as its
        parent, and with a device+inode identity that still matches when reopened
        no-follow under the pinned fd. ANY verification failure (a typed path
        rejection or a raw ``lstat``/``open`` error) reclaims the stage and raises
        ``ScratchPathUnsafe`` with a value-free message, so a race can never leave
        an unverified stage on disk (never a bare ``OSError`` carrying native
        detail)."""

        try:
            st = os.lstat(stage_name, dir_fd=dir_fd)
            if _stat.S_ISLNK(st.st_mode):
                raise ScratchPathUnsafe(message="publication stage is a symlink")
            if not _stat.S_ISDIR(st.st_mode):
                raise ScratchPathUnsafe(message="publication stage is not a directory")
            if st.st_dev != parent_dev:
                raise ScratchPathUnsafe(
                    message="publication stage is not on the destination device"
                )
            if st.st_uid != os.geteuid():
                raise ScratchPathUnsafe(message="publication stage is not owned by this principal")
            if st.st_mode & 0o777 != 0o700:
                raise ScratchPathUnsafe(message="publication stage mode is not 0700")
            # Reopen the stage no-follow under the pinned fd and confirm its
            # device+inode identity is unchanged: a name swapped for a link or a
            # different inode between lstat and acceptance is caught here.
            stage_fd = os.open(
                stage_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=dir_fd
            )
            try:
                ident = os.fstat(stage_fd)
            finally:
                os.close(stage_fd)
            if (ident.st_dev, ident.st_ino) != (st.st_dev, st.st_ino):
                raise ScratchPathUnsafe(
                    message="publication stage identity changed under the pinned fd"
                )
        except ScratchPathUnsafe:
            self._reclaim_stage(dir_fd, stage_name)
            raise
        except OSError:
            self._reclaim_stage(dir_fd, stage_name)
            raise ScratchPathUnsafe(message="publication stage failed verification") from None

    @staticmethod
    def _reclaim_stage(dir_fd: int, stage_name: str) -> None:
        """Remove a rejected stage by name under the pinned parent fd, never by a
        re-resolved path: whether it is still our empty directory or was swapped
        for a link/file, it is unlinked in place without following it."""

        for remover in (os.rmdir, os.unlink):
            try:
                remover(stage_name, dir_fd=dir_fd)
                return
            except OSError:
                continue

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
        kind = (
            getattr(resource, "kind", ResourceKind.FILE).value
            if hasattr(resource, "kind")
            else "publication"
        )
        return self._quota_report(
            observation,
            resource_id=resource.resource_id,
            kind=kind,
            dimension=dimension,
            errno=errno,
            evidence="errno",
        )

    def attempt_quota_report(self, *, errno: int | None) -> QuotaReport:
        """An attempt-level quota report for an errno-classified trip surfaced
        where no one resource is the obvious owner. Errno-only; use
        ``classify_attempt_io`` for a provider failure that lost its errno."""

        observation = self._provider.observe(self._root, self._policy)
        dimension = "inodes" if errno == _errno.EDQUOT and _inode_pressure(observation) else "bytes"
        return self._quota_report(
            observation, resource_id="", kind="attempt", dimension=dimension, errno=errno
        )

    def classify_attempt_io(self, *, errno: int | None) -> QuotaReport:
        """Classify an attempt-level I/O failure that may carry no errno into the
        shared report (plan 1.5 item 5): DuckDB and libpq ``COPY`` wrap a kernel
        quota trip in a provider exception with the errno stripped, so errno
        classification alone misses them.

        Tiered, and never parses a native exception string:

        * errno preserved and a quota errno -> ``errno`` evidence.
        * no quota errno -> a bounded authoritative kernel probe. EDQUOT/ENOSPC on
          the probe is ``provider_quota`` evidence for that dimension.
        * neither -> ``unknown_io``: a write failed with no evidence it was a
          quota breach, so one is not claimed.

        The caller decides what to raise: a quota-coded failure for ``errno`` /
        ``provider_quota``, an honest pass-through or typed outcome for
        ``unknown_io``."""

        observation = self._provider.observe(self._root, self._policy)
        if errno in _QUOTA_ERRNOS:
            dimension = (
                "inodes" if errno == _errno.EDQUOT and _inode_pressure(observation) else "bytes"
            )
            return self._quota_report(
                observation, resource_id="", kind="attempt", dimension=dimension, errno=errno
            )
        exhausted, probe_dim = self._provider.probe_exhaustion(self._root, self._policy)
        if exhausted:
            return self._quota_report(
                observation,
                resource_id="",
                kind="attempt",
                dimension=probe_dim or "bytes",
                errno=None,
                evidence="provider_quota",
            )
        return self._quota_report(
            observation,
            resource_id="",
            kind="attempt",
            dimension="unknown",
            errno=None,
            evidence="unknown_io",
        )

    def _quota_report(
        self,
        observation: QuotaObservation,
        *,
        resource_id: str,
        kind: str,
        dimension: str,
        errno: int | None,
        evidence: str = "errno",
    ) -> QuotaReport:
        return QuotaReport(
            arena_id=self._arena_id,
            resource_id=resource_id,
            kind=kind,
            effective_bytes_cap=self._policy.quota_bytes,
            effective_inodes_cap=self._policy.quota_inodes,
            observation=observation,
            dimension=dimension,
            disposition=Disposition.RETAINED,
            errno=errno,
            evidence=evidence,
        )


def _inode_pressure(observation: QuotaObservation) -> bool:
    used = observation.inodes_used
    return used is not None and used >= observation.inodes_limit

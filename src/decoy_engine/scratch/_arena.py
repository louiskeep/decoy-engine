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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from decoy_engine.scratch._capabilities import (
    DIR_MODE,
    FILE_MODE,
    ArenaCapability,
    CheckpointRef,
    Identity,
    NativeDirectory,
    PublicationStage,
    RegisteredFile,
    open_verified_dir,
    open_verified_target,
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


@dataclass(frozen=True, slots=True)
class _Allocated:
    """What the arena records for one allocated resource, so ``release`` can
    re-verify the target's pinned kernel identity before any destructive reclaim
    (argon use-time model): the arena trusts its own record, not a path or an
    identity carried back on the capability it is handed."""

    path: Path
    parent_identity: Identity
    identity: Identity
    expected_mode: int


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
        root_identity: Identity,
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
        # The arena root identity, pinned once by the manager when it created and
        # verified the arena (never re-stat'd here). Every allocation re-opens the
        # root O_NOFOLLOW and re-asserts this identity before any *at call.
        self._root_identity = root_identity
        self._paths: dict[str, _Allocated] = {}

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
        # Record the arena root's pinned identity, never a fresh unverified
        # os.stat(self._root) (which would follow a symlink swapped in before
        # allocation and forge the ledger's (st_dev, st_ino)).
        root_dev, root_ino = self._root_identity
        self._ledger.register_resource(
            resource_id=resource_id,
            arena_id=self._arena_id,
            lease=self._lease,
            kind=kind,
            retention=retention,
            owner_principal=self._owner,
            root_dev=root_dev,
            root_ino=root_ino,
            quota_domain_id=self._quota_domain_id,
            recovery_class=retention.value,
        )

    def _create_verified_child(self, name: str, *, is_dir: bool) -> tuple[Path, Identity, Identity]:
        """Create a generated component beneath the arena root RELATIVE to a
        no-follow root fd and capture its pinned ``(st_dev, st_ino)`` identity
        (argon). Replaces the old ``.resolve()`` getter: the root is reopened
        ``O_NOFOLLOW`` and its pinned identity re-asserted (so the root cannot
        have been swapped since ``Arena`` init), the child is created with
        ``dir_fd=root_fd`` (``O_CREAT|O_EXCL|O_NOFOLLOW`` for a file, ``mkdir``
        for a dir), and its identity/owner/mode verified under the same pinned
        fd. Returns ``(path, parent_identity, child_identity)``. Any failure
        reclaims a partial child and raises ``ScratchPathUnsafe``.
        """

        if "/" in name or name in ("", ".", ".."):
            raise ScratchPathUnsafe(message="resource component is unsafe")
        child = self._root / name
        root_fd = os.open(self._root, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
        try:
            rst = os.fstat(root_fd)
            if (rst.st_dev, rst.st_ino) != self._root_identity:
                raise ScratchPathUnsafe(message="arena root identity changed since init")
            if rst.st_uid != os.geteuid():
                raise ScratchPathUnsafe(message="arena root is not owned by this principal")
            parent_identity = (rst.st_dev, rst.st_ino)
            if is_dir:
                os.mkdir(name, mode=0o700, dir_fd=root_fd)
                child_identity = self._verified_dir_identity(root_fd, name, rst.st_dev)
            else:
                # 0600 from creation, O_EXCL so we never adopt a planted file,
                # O_NOFOLLOW so a symlink planted at the name is refused.
                fd = os.open(
                    name,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=root_fd,
                )
                try:
                    cst = os.fstat(fd)
                finally:
                    os.close(fd)
                if (
                    cst.st_dev != rst.st_dev
                    or cst.st_uid != os.geteuid()
                    or cst.st_mode != FILE_MODE
                ):
                    self._reclaim_stage(root_fd, name)
                    raise ScratchPathUnsafe(message="arena file failed verification")
                child_identity = (cst.st_dev, cst.st_ino)
            return child, parent_identity, child_identity
        except ScratchPathUnsafe:
            raise
        except OSError:
            self._reclaim_stage(root_fd, name)
            raise ScratchPathUnsafe(message="arena child creation failed verification") from None
        finally:
            os.close(root_fd)

    def allocate_file(self, kind: ResourceKind, opaque_name: str) -> RegisteredFile:
        resource_id = self._new_resource_id()
        self._register(resource_id, kind, self._policy.retention)
        path, parent_id, child_id = self._create_verified_child(resource_id, is_dir=False)
        self._paths[resource_id] = _Allocated(path, parent_id, child_id, FILE_MODE)
        self._ledger.transition(
            resource_id,
            frm=ResourceState.CREATING,
            to=ResourceState.ACTIVE,
            generation=self._lease.generation,
        )
        return RegisteredFile(
            arena_id=self._arena_id,
            resource_id=resource_id,
            kind=kind,
            _writable_path=path,
            _parent_identity=parent_id,
            _identity=child_id,
            _expected_mode=FILE_MODE,
        )

    def allocate_native_dir(self, kind: ResourceKind, writer_id: str) -> NativeDirectory:
        resource_id = self._new_resource_id()
        self._register(resource_id, ResourceKind.NATIVE_DIR, self._policy.retention)
        path, parent_id, child_id = self._create_verified_child(resource_id, is_dir=True)
        self._paths[resource_id] = _Allocated(path, parent_id, child_id, DIR_MODE)
        self._ledger.transition(
            resource_id,
            frm=ResourceState.CREATING,
            to=ResourceState.ACTIVE,
            generation=self._lease.generation,
        )
        return NativeDirectory(
            arena_id=self._arena_id,
            resource_id=resource_id,
            writer_id=writer_id,
            _native_root=path,
            _parent_identity=parent_id,
            _identity=child_id,
            _expected_mode=DIR_MODE,
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
        # Stage beside the destination, in its parent directory. A missing or
        # symlinked parent is caught when it is opened O_NOFOLLOW|O_DIRECTORY
        # below (no Path.exists() pre-check, which would re-resolve the path).
        dest_parent = destination.parent
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
            parent_stat = os.fstat(dir_fd)
            parent_identity: Identity = (parent_stat.st_dev, parent_stat.st_ino)
            self._register(resource_id, ResourceKind.PUBLICATION, Retention.DURABLE_PUBLICATION)
            os.mkdir(stage_name, mode=0o700, dir_fd=dir_fd)
            stage_identity = self._verify_stage(dir_fd, stage_name, parent_stat.st_dev)
        finally:
            os.close(dir_fd)
        self._paths[resource_id] = _Allocated(stage, parent_identity, stage_identity, DIR_MODE)
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
            _parent_identity=parent_identity,
            _identity=stage_identity,
            _expected_mode=DIR_MODE,
        )

    def _verify_stage(self, dir_fd: int, stage_name: str, parent_dev: int) -> Identity:
        """Verify a freshly created publication stage and return its pinned
        ``(st_dev, st_ino)`` identity. ANY verification failure (a typed path
        rejection or a raw ``lstat``/``open`` error) reclaims the stage and raises
        ``ScratchPathUnsafe`` with a value-free message, so a race can never leave
        an unverified stage on disk (never a bare ``OSError`` carrying native
        detail)."""

        try:
            return self._verified_dir_identity(dir_fd, stage_name, parent_dev)
        except ScratchPathUnsafe:
            self._reclaim_stage(dir_fd, stage_name)
            raise
        except OSError:
            self._reclaim_stage(dir_fd, stage_name)
            raise ScratchPathUnsafe(message="publication stage failed verification") from None

    @staticmethod
    def _verified_dir_identity(dir_fd: int, name: str, parent_dev: int) -> Identity:
        """Verify a freshly created directory child the way the data arena
        verifies a child (plan 1.5), all relative to the pinned parent fd:
        non-symlink, owned by this principal, mode 0700, same device as its
        parent, and with a device+inode identity that still matches when reopened
        no-follow under the pinned fd. Returns the pinned ``(st_dev, st_ino)``.
        Raises ``ScratchPathUnsafe`` on any typed mismatch (the caller maps a raw
        ``OSError`` and performs reclaim)."""

        st = os.lstat(name, dir_fd=dir_fd)
        if _stat.S_ISLNK(st.st_mode):
            raise ScratchPathUnsafe(message="directory child is a symlink")
        if not _stat.S_ISDIR(st.st_mode):
            raise ScratchPathUnsafe(message="directory child is not a directory")
        if st.st_dev != parent_dev:
            raise ScratchPathUnsafe(message="directory child is not on the parent device")
        if st.st_uid != os.geteuid():
            raise ScratchPathUnsafe(message="directory child is not owned by this principal")
        if st.st_mode & 0o777 != 0o700:
            raise ScratchPathUnsafe(message="directory child mode is not 0700")
        # Reopen the child no-follow under the pinned fd and confirm its
        # device+inode identity is unchanged: a name swapped for a link or a
        # different inode between lstat and acceptance is caught here.
        child_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=dir_fd)
        try:
            ident = os.fstat(child_fd)
        finally:
            os.close(child_fd)
        if (ident.st_dev, ident.st_ino) != (st.st_dev, st.st_ino):
            raise ScratchPathUnsafe(message="directory child identity changed under the pinned fd")
        return (ident.st_dev, ident.st_ino)

    @staticmethod
    def _reclaim_stage(dir_fd: int, stage_name: str) -> None:
        """Remove a rejected stage/child by name under the pinned parent fd and
        CONFIRM it is gone, never by a re-resolved path (argon guaranteed-reclaim).

        The old implementation swallowed every ``OSError`` and returned, so a
        racer-populated directory or a permission change could leave an
        unverified entry on disk silently, violating the "nothing left on disk"
        invariant. This version removes relative to the pinned fd, then confirms
        the entry no longer resolves under that fd; if a non-empty directory
        blocks ``rmdir`` it clears the children it owns (no-follow, same device)
        and retries once, and if removal still cannot be confirmed it raises
        ``ScratchPathUnsafe`` rather than returning as if clean."""

        for remover in (os.rmdir, os.unlink):
            try:
                remover(stage_name, dir_fd=dir_fd)
                break
            except FileNotFoundError:
                return
            except OSError:
                continue
        if _entry_absent(dir_fd, stage_name):
            return
        _clear_owned_dir_children(dir_fd, stage_name)
        try:
            os.rmdir(stage_name, dir_fd=dir_fd)
        except OSError:
            pass
        if _entry_absent(dir_fd, stage_name):
            return
        raise ScratchPathUnsafe(
            message="publication stage could not be reclaimed after failed verification"
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
        payload, parent_id, child_id = self._create_verified_child(resource_id, is_dir=True)
        self._paths[resource_id] = _Allocated(payload, parent_id, child_id, DIR_MODE)
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
            _parent_identity=parent_id,
            _identity=child_id,
            _expected_mode=DIR_MODE,
        )

    def release(
        self, resource: RegisteredFile | NativeDirectory | PublicationStage | CheckpointRef
    ) -> CleanupReport:
        """Release one resource. Capacity is reported released only after the
        provider confirms reclamation; a failed cleanup keeps its charge and
        inventory and raises ``ScratchCleanupError`` (plan 1.5 item 6).

        The destructive reclaim is identity-bound: the arena hands the provider
        the target's pinned parent and child ``(st_dev, st_ino)`` from its OWN
        record (not the capability it was handed), and the provider re-verifies
        and deletes on the SAME ``O_NOFOLLOW`` file descriptors, so there is no
        path re-resolution between the check and the delete. A post-allocation
        symlink or inode swap of the parent or target fails closed with
        ``ScratchPathUnsafe`` and nothing outside the pinned object is touched
        (argon)."""

        resource_id = resource.resource_id
        record = self._paths.get(resource_id)
        if record is None:
            self._ledger.set_disposition(
                resource_id, disposition=Disposition.DELETED.value, state=ResourceState.REAPED
            )
            return CleanupReport(
                arena_id=self._arena_id, resource_id=resource_id, disposition=Disposition.DELETED
            )
        # Confirm absence under the verified parent fd (never Path.exists(), which
        # follows symlinks and would mark a swapped-in broken link as DELETED).
        with open_verified_dir(
            record.path.parent, record.parent_identity, require_owner=False
        ) as vparent:
            if _entry_absent(vparent.fd, record.path.name):
                self._ledger.set_disposition(
                    resource_id, disposition=Disposition.DELETED.value, state=ResourceState.REAPED
                )
                return CleanupReport(
                    arena_id=self._arena_id,
                    resource_id=resource_id,
                    disposition=Disposition.DELETED,
                )
        # The reclaim runs INSIDE open_verified_target so the delete executes on
        # the same O_NOFOLLOW parent fd the identity was verified against.
        with open_verified_target(
            record.path, record.parent_identity, record.identity, record.expected_mode
        ) as vtarget:
            confirmed, retained_bytes, retained_inodes = self._provider.reclaim(vtarget)
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

    def _observe(self) -> QuotaObservation:
        """Observe usage on the arena root through a verified-dir handle, so the
        provider walks the ``fstat``-verified fd, never a re-resolved path."""

        with open_verified_dir(self._root, self._root_identity) as vroot:
            return self._provider.observe(vroot, self._policy)

    def _probe_exhaustion(self) -> tuple[bool, str]:
        """Probe the kernel hard cap on the arena root through a verified-dir
        handle (the probe file is created/unlinked ``dir_fd=vroot.fd``)."""

        with open_verified_dir(self._root, self._root_identity) as vroot:
            return self._provider.probe_exhaustion(vroot, self._policy)

    def build_quota_report(
        self,
        resource: RegisteredFile | NativeDirectory | PublicationStage,
        *,
        errno: int | None,
    ) -> QuotaReport:
        observation = self._observe()
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

        observation = self._observe()
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

        observation = self._observe()
        if errno in _QUOTA_ERRNOS:
            dimension = (
                "inodes" if errno == _errno.EDQUOT and _inode_pressure(observation) else "bytes"
            )
            return self._quota_report(
                observation, resource_id="", kind="attempt", dimension=dimension, errno=errno
            )
        exhausted, probe_dim = self._probe_exhaustion()
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


def _entry_absent(dir_fd: int, name: str) -> bool:
    """True iff ``name`` no longer resolves under the pinned parent fd. A raw
    ``OSError`` other than ``FileNotFoundError`` means the entry's fate could not
    be confirmed, so it is treated as still present (reclaim must raise)."""

    try:
        os.lstat(name, dir_fd=dir_fd)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _clear_owned_dir_children(dir_fd: int, name: str) -> None:
    """Best-effort removal of the children of a stage directory a racer populated
    between ``mkdir`` and verification, so a non-empty directory can still be
    reclaimed. Opens the directory ``O_NOFOLLOW`` under the pinned parent fd,
    refuses to cross a device boundary, and unlinks each child relative to the
    directory fd without following a symlink."""

    try:
        stage_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=dir_fd)
    except OSError:
        return
    try:
        stage_dev = os.fstat(stage_fd).st_dev
        for child in os.listdir(stage_fd):
            try:
                cst = os.lstat(child, dir_fd=stage_fd)
                if cst.st_dev != stage_dev:
                    continue
                if _stat.S_ISDIR(cst.st_mode):
                    _clear_owned_dir_children(stage_fd, child)
                    os.rmdir(child, dir_fd=stage_fd)
                else:
                    os.unlink(child, dir_fd=stage_fd)
            except OSError:
                continue
    finally:
        os.close(stage_fd)

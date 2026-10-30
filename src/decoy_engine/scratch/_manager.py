"""The ScratchManager: the single runtime choke-point for temporary storage.

All temporary producers obtain a ``RegisteredFile``, ``NativeDirectory`` or
``PublicationStage`` from an ``Arena`` this manager issues; no lower-level API
may opt out by taking an arbitrary directory (plan 2). Before a capability is
issued, the manager: obtains the read-only lease identity from the ownership
authority, qualifies the backend (fail closed otherwise), bootstraps the pool's
one durable control ledger, creates a restrictive data arena and verifies the
kernel project quota the deployment adapter assigned.

The manager never mints or advances a fence and never deletes on its own clock;
the ownership authority is the sole source of truth and the engine ledger is its
subordinate cache (ledger subordination contract). ``sweep`` is the bounded,
caller-scheduled entrypoint; Slice 1 implements its safe, retain-by-default
decision and the destructive orphan recovery lands in Slice 5.
"""

from __future__ import annotations

import getpass
import os
import uuid
from pathlib import Path

from decoy_engine.scratch._arena import Arena
from decoy_engine.scratch._authority import AuthorityUnavailable, OwnershipAuthority
from decoy_engine.scratch._capabilities import open_verified_dir
from decoy_engine.scratch._errors import ScratchBackendUnavailable
from decoy_engine.scratch._ledger import ResourceLedger, ResourceRow
from decoy_engine.scratch._policy import (
    SWEEP_INELIGIBLE_STATES,
    AuthorityMode,
    LeaseIdentity,
    ScratchPolicy,
)
from decoy_engine.scratch._provider import StorageProvider
from decoy_engine.scratch._reports import Disposition, SweepReport, SweptResource

_CONTROL_DIRNAME = ".decoy-control"


class ScratchManager:
    """Pool-level manager holding the one durable control ledger."""

    def __init__(self, *, ledger: ResourceLedger, control_arena: Path, pool_id: str) -> None:
        self._ledger = ledger
        self._control_arena = control_arena
        self._pool_id = pool_id

    @classmethod
    def open_pool(
        cls, *, pool_id: str, control_arena: Path, provider: StorageProvider
    ) -> ScratchManager:
        """Bootstrap the one control ledger for a managed pool.

        ``control_arena`` is a control-owned 0700, quota-reserved arena the
        deployment adapter provisioned ahead of time (plan 1.5). Its own files
        are registered as ``CONTROL`` resources so the manager never creates a
        recursive scratch exemption for its own metadata (plan 1.3). The control
        arena uses its own quota domain, distinct from any data arena, so
        recording a terminal data-quota breach can never fail on control quota.
        """

        # Create and verify the control arena relative to its (existing) parent
        # fd: mkdir under a no-follow parent, then fchmod/fstat on the child fd,
        # never a path chmod/stat that would follow a swapped-in symlink. The
        # deployment adapter provisions the control arena ahead of time in
        # production; here we also create it when absent (its parent must exist).
        control_dev, control_ino = _bootstrap_control_dir(control_arena)
        ledger = ResourceLedger.bootstrap(control_arena, pool_id)
        control_lease = LeaseIdentity(
            job_id=f"control::{pool_id}",
            attempt_id="control",
            lease_id=f"control-lease::{pool_id}",
            generation=0,
            mode=AuthorityMode.TARGET_LOCK,
            local_stage_root=str(control_arena),
        )
        control_arena_id = f"control-{uuid.uuid4().hex}"
        if not any(a.is_control for a in ledger.list_arenas()):
            ledger.register_arena(
                arena_id=control_arena_id,
                lease=control_lease,
                root_path=str(control_arena),
                root_dev=control_dev,
                root_ino=control_ino,
                quota_domain_id=f"{pool_id}::control",
                quota_bytes=1 << 20,
                quota_inodes=256,
                is_control=True,
            )
        return cls(ledger=ledger, control_arena=control_arena, pool_id=pool_id)

    def open_attempt(
        self,
        policy: ScratchPolicy,
        storage_provider: StorageProvider,
        ownership_authority: OwnershipAuthority,
    ) -> Arena:
        """Open an attempt-scoped arena, or fail closed.

        Ordering is load-bearing: authority identity, backend qualification,
        durable arena registration and quota verification all precede the first
        writable capability (plan 2). Any step that cannot prove its guarantee
        raises rather than degrading to unprotected storage.
        """

        # 1. Ownership identity (read-only; the engine never mints it).
        lease = ownership_authority.lease_for(
            policy.job_id, policy.attempt_id, policy.labels.get("local_stage_root", "")
        )
        root = (
            Path(lease.local_stage_root) if lease.local_stage_root else self._control_arena.parent
        )

        # 2. Qualify the backend on an opened, identity-verified root fd; no
        #    permissive fallback (plan 1.5, A13). The root is opened once and the
        #    verified descriptor is carried into qualify and create_arena, so the
        #    path is never re-resolved between checks.
        arena_id = uuid.uuid4().hex
        with open_verified_dir(root, require_owner=False) as vroot:
            qualification = storage_provider.qualify(vroot)
            if not qualification.qualified:
                raise ScratchBackendUnavailable(
                    message="storage backend did not qualify", reasons=qualification.reasons
                )
            # 3. Create the restrictive data arena under the verified root fd; the
            #    provider returns the child path and its pinned (st_dev, st_ino).
            arena_root, arena_identity = storage_provider.create_arena(vroot, arena_id)

        # 4. Verify the kernel quota on the newly created arena, opened and pinned
        #    by the identity create_arena returned (no intervening path re-stat).
        with open_verified_dir(arena_root, arena_identity) as varena:
            storage_provider.verify_quota(varena, policy)

        # 5. Register the arena durably before issuing any capability, recording
        #    the pinned identity rather than a fresh os.stat(arena_root).
        quota_domain_id = f"{policy.job_id}::{policy.attempt_id}"
        self._ledger.register_arena(
            arena_id=arena_id,
            lease=lease,
            root_path=str(arena_root),
            root_dev=arena_identity[0],
            root_ino=arena_identity[1],
            quota_domain_id=quota_domain_id,
            quota_bytes=policy.quota_bytes,
            quota_inodes=policy.quota_inodes,
            is_control=False,
        )
        return Arena(
            manager=self,
            ledger=self._ledger,
            provider=storage_provider,
            policy=policy,
            lease=lease,
            arena_id=arena_id,
            root=arena_root,
            root_identity=arena_identity,
            quota_domain_id=quota_domain_id,
            owner_principal=_current_principal(),
        )

    def sweep(self, scope: str, authority: OwnershipAuthority) -> SweepReport:
        """Bounded, conservative sweep decision (Slice 1: retain-by-default).

        For each registered non-control arena, the manager reads the durable
        state and asks the authority whether the owning lease is reap-eligible.
        A resource is retained unless: the authority says reap-eligible, its
        state is not sweep-ineligible (valid checkpoint / publication / unknown)
        and the exclusive liveness lock can be taken. Lease expiry alone is only
        candidate selection; PID/mtime/prefix are never deletion authority (plan
        1.8). The destructive claim-and-delete loop and its A07/A08 crash matrix
        are Slice 5, so this slice performs no deletion and reports the decision.
        """

        decisions: list[SweptResource] = []
        for arena in self._ledger.list_arenas():
            if arena.is_control:
                continue
            lease = LeaseIdentity(
                job_id=arena.job_id,
                attempt_id=arena.attempt_id,
                lease_id=arena.lease_id,
                generation=arena.generation,
                mode=arena.authority_mode,
                local_stage_root=arena.root_path,
            )
            try:
                verdict = authority.reap_verdict(lease)
            except AuthorityUnavailable:
                decisions.append(
                    SweptResource(
                        arena_id=arena.arena_id,
                        resource_id="*",
                        disposition=Disposition.RETAINED,
                        reason="authority unavailable",
                    )
                )
                continue
            for resource in self._ledger.list_resources(arena.arena_id):
                decisions.append(self._decide(arena.arena_id, resource, verdict.reap_eligible))
        return SweepReport(scope=scope, resources=tuple(decisions))

    @staticmethod
    def _decide(arena_id: str, resource: ResourceRow, reap_eligible: bool) -> SweptResource:
        if resource.state in SWEEP_INELIGIBLE_STATES:
            reason = f"state {resource.state.value} is never swept on lease expiry"
            return SweptResource(
                arena_id=arena_id,
                resource_id=resource.resource_id,
                disposition=Disposition.RETAINED,
                reason=reason,
            )
        if not reap_eligible:
            return SweptResource(
                arena_id=arena_id,
                resource_id=resource.resource_id,
                disposition=Disposition.RETAINED,
                reason="authority: lease not reap-eligible",
            )
        # Reap-eligible candidate. Slice 1 does not delete (Slice 5 owns the
        # exclusive-lock claim and destructive loop); report it as a candidate.
        return SweptResource(
            arena_id=arena_id,
            resource_id=resource.resource_id,
            disposition=Disposition.RETAINED,
            reason="reap-eligible candidate; deletion deferred to Slice 5",
        )

    @property
    def ledger(self) -> ResourceLedger:
        return self._ledger

    def close(self) -> None:
        self._ledger.close()


def _bootstrap_control_dir(control_arena: Path) -> tuple[int, int]:
    """Create (if absent) and verify the control arena relative to its parent fd,
    returning its pinned ``(st_dev, st_ino)``. The parent is opened
    ``O_NOFOLLOW|O_DIRECTORY`` and the child is created/opened/chmod'd/stat'd via
    that fd, so a symlink swapped in for the control arena is refused rather than
    followed. The parent directory must already exist (the deployment adapter
    provisions the control location)."""

    name = control_arena.name
    parent_fd = os.open(control_arena.parent, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    try:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=parent_fd)
        try:
            os.fchmod(fd, 0o700)
            st = os.fstat(fd)
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)
    return st.st_dev, st.st_ino


def _current_principal() -> str:
    try:
        return getpass.getuser()
    except (OSError, KeyError):
        return f"uid:{os.getuid()}"

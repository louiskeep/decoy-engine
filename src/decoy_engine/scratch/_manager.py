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

        control_arena.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(control_arena, 0o700)
        ledger = ResourceLedger.bootstrap(control_arena, pool_id)
        control_lease = LeaseIdentity(
            job_id=f"control::{pool_id}",
            attempt_id="control",
            lease_id=f"control-lease::{pool_id}",
            generation=0,
            mode=AuthorityMode.TARGET_LOCK,
            local_stage_root=str(control_arena),
        )
        stat = os.stat(control_arena)
        control_arena_id = f"control-{uuid.uuid4().hex}"
        if not any(a.is_control for a in ledger.list_arenas()):
            ledger.register_arena(
                arena_id=control_arena_id,
                lease=control_lease,
                root_path=str(control_arena),
                root_dev=stat.st_dev,
                root_ino=stat.st_ino,
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

        # 2. Qualify the backend; no permissive fallback (plan 1.5, A13).
        qualification = storage_provider.qualify(root)
        if not qualification.qualified:
            raise ScratchBackendUnavailable(
                message="storage backend did not qualify", reasons=qualification.reasons
            )

        # 3. Create the restrictive data arena and verify the kernel quota.
        arena_id = uuid.uuid4().hex
        arena_root = storage_provider.create_arena(root, arena_id)
        storage_provider.verify_quota(arena_root, policy)

        # 4. Register the arena durably before issuing any capability.
        stat = os.stat(arena_root)
        quota_domain_id = f"{policy.job_id}::{policy.attempt_id}"
        self._ledger.register_arena(
            arena_id=arena_id,
            lease=lease,
            root_path=str(arena_root),
            root_dev=stat.st_dev,
            root_ino=stat.st_ino,
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


def _current_principal() -> str:
    try:
        return getpass.getuser()
    except (OSError, KeyError):
        return f"uid:{os.getuid()}"

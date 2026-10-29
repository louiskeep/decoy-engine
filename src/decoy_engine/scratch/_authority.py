"""The ownership-authority seam and its two frozen implementations.

The platform ``HostJobLease`` row is the sole authority for whether an attempt
owns its resources; the engine's local ledger is a subordinate cache (ledger
subordination contract 2.1). The engine never mints, renews, expires or
adjudicates ownership: it asks the authority for the read-only lease identity,
verifies it is still current, and asks whether a resource is reap-eligible.

Because the designated durable authority is flag-gated off today, the seam has
two implementations that make the same decision (contract 2.5):

* ``PlatformLeaseAuthority`` binds to ``host_lease_authority`` +
  ``host_lease_recovery`` + ``lease_recovery_engine`` when the platform flag is
  on. It is flag-gated off by default and raises when used while disabled.
* ``TargetLockAuthority`` treats the live ``JobTargetLock`` + ``reaper`` +
  terminal-job state as the ownership signal.

The engine is a library and must not import the platform, so each binds to an
injected client object that conforms to a small Protocol here. The platform
worker supplies the concrete client; Slice 1 exercises both against doubles.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from decoy_engine.scratch._errors import ScratchError
from decoy_engine.scratch._policy import AuthorityMode, LeaseIdentity


class AuthorityUnavailable(ScratchError):  # noqa: N818 -- design-named F25 failure type (plan 1.8)
    """The ownership authority could not be reached or returned an unverifiable
    answer. Every caller treats this as retain / fail-closed: a resource is
    never deleted on an unavailable authority (plan 1.8, contract 2.4)."""

    def __init__(self, *, message: str = "") -> None:
        super().__init__(code="scratch_authority_unavailable", message=message)


class LeaseAuthorityDisabledError(ScratchError):
    """The platform lease authority is flag-gated off. Mirrors the platform's
    own ``LeaseAuthorityDisabledError`` so enabling the flag is a platform-side
    switch, not an engine change (contract 2.5)."""

    def __init__(self, *, message: str = "") -> None:
        super().__init__(code="scratch_lease_authority_disabled", message=message)


@dataclass(frozen=True, slots=True)
class ReapVerdict:
    """Whether the authority says a resource's owning lease is reap-eligible.

    ``reap_eligible`` True is only candidate selection; the sweep still needs the
    exclusive liveness lock and a no-valid-checkpoint/publication check before
    any deletion (plan 1.8, contract 2.4)."""

    reap_eligible: bool
    reason: str


@runtime_checkable
class OwnershipAuthority(Protocol):
    """Read-mostly ownership seam the engine binds to, never an implementation
    directly (contract 2.5)."""

    @property
    def mode(self) -> AuthorityMode: ...

    def lease_for(self, job_id: str, attempt_id: str, local_stage_root: str) -> LeaseIdentity:
        """Return the current ownership tuple for an attempt. Raises
        ``AuthorityUnavailable`` if the authority cannot be reached."""
        ...

    def verify(self, lease: LeaseIdentity) -> bool:
        """True iff ``lease`` is still the current owner at its generation."""
        ...

    def reap_verdict(self, lease: LeaseIdentity) -> ReapVerdict:
        """Whether the owning lease is reap-eligible per the authority."""
        ...


# --- Injected client Protocols (the platform supplies the concrete objects) --


@runtime_checkable
class PlatformLeaseClient(Protocol):
    """The subset of ``host_lease_authority`` + recovery the engine consumes."""

    def claim(self, job_id: str, attempt_id: str) -> tuple[str, int]:
        """Return ``(lease_id, generation)`` for the attempt, or raise."""
        ...

    def is_current(self, job_id: str, lease_id: str, generation: int) -> bool: ...

    def recover_verdict(self, job_id: str, lease_id: str, generation: int) -> tuple[bool, str]:
        """Return ``(reap_eligible, reason)`` from
        ``recover_lease_and_reconcile`` / ``reap_once``."""
        ...


@runtime_checkable
class TargetLockClient(Protocol):
    """The subset of ``JobTargetLock`` + ``reaper`` + job-state the engine
    consumes while the lease-authority flag is off."""

    def lease_tuple(self, job_id: str, attempt_id: str) -> tuple[str, int]:
        """Return ``(lease_id, generation)`` derived from the live target-lock
        and scheduler state, or raise."""
        ...

    def holds_target_lock(self, job_id: str, lease_id: str) -> bool: ...

    def released_and_terminal(self, job_id: str, lease_id: str) -> tuple[bool, str]:
        """``(reap_eligible, reason)``: the worker's target locks were released
        (``release_orphaned_terminal_locks`` / ``reap_stale_workers``) and the
        job is terminal."""
        ...


class PlatformLeaseAuthority:
    """Ownership authority bound to the platform's durable lease authority.

    Flag-gated off by default (``enabled=False``), mirroring
    ``settings.adaptive_scheduler_lease_authority_enabled``; every operation
    raises ``LeaseAuthorityDisabledError`` while off.
    """

    def __init__(self, client: PlatformLeaseClient, *, enabled: bool = False) -> None:
        self._client = client
        self._enabled = enabled

    @property
    def mode(self) -> AuthorityMode:
        return AuthorityMode.PLATFORM_LEASE

    def _require_enabled(self) -> None:
        if not self._enabled:
            raise LeaseAuthorityDisabledError(message="platform lease authority is flag-gated off")

    def lease_for(self, job_id: str, attempt_id: str, local_stage_root: str) -> LeaseIdentity:
        self._require_enabled()
        try:
            lease_id, generation = self._client.claim(job_id, attempt_id)
        except Exception as exc:
            raise AuthorityUnavailable(message="platform lease claim failed") from exc
        return LeaseIdentity(
            job_id=job_id,
            attempt_id=attempt_id,
            lease_id=lease_id,
            generation=generation,
            mode=AuthorityMode.PLATFORM_LEASE,
            local_stage_root=local_stage_root,
        )

    def verify(self, lease: LeaseIdentity) -> bool:
        self._require_enabled()
        try:
            return self._client.is_current(lease.job_id, lease.lease_id, lease.generation)
        except Exception as exc:
            raise AuthorityUnavailable(message="platform lease verify failed") from exc

    def reap_verdict(self, lease: LeaseIdentity) -> ReapVerdict:
        self._require_enabled()
        try:
            eligible, reason = self._client.recover_verdict(
                lease.job_id, lease.lease_id, lease.generation
            )
        except Exception as exc:
            raise AuthorityUnavailable(message="platform recovery verdict failed") from exc
        return ReapVerdict(reap_eligible=eligible, reason=reason)


class TargetLockAuthority:
    """Ownership authority bound to the live ``JobTargetLock`` + reaper path.

    This is the authority that actually runs in production until the platform
    lease-authority flag is enabled (contract 1.2, 2.5).
    """

    def __init__(self, client: TargetLockClient) -> None:
        self._client = client

    @property
    def mode(self) -> AuthorityMode:
        return AuthorityMode.TARGET_LOCK

    def lease_for(self, job_id: str, attempt_id: str, local_stage_root: str) -> LeaseIdentity:
        try:
            lease_id, generation = self._client.lease_tuple(job_id, attempt_id)
        except Exception as exc:
            raise AuthorityUnavailable(message="target-lock lease lookup failed") from exc
        return LeaseIdentity(
            job_id=job_id,
            attempt_id=attempt_id,
            lease_id=lease_id,
            generation=generation,
            mode=AuthorityMode.TARGET_LOCK,
            local_stage_root=local_stage_root,
        )

    def verify(self, lease: LeaseIdentity) -> bool:
        try:
            return self._client.holds_target_lock(lease.job_id, lease.lease_id)
        except Exception as exc:
            raise AuthorityUnavailable(message="target-lock verify failed") from exc

    def reap_verdict(self, lease: LeaseIdentity) -> ReapVerdict:
        try:
            eligible, reason = self._client.released_and_terminal(lease.job_id, lease.lease_id)
        except Exception as exc:
            raise AuthorityUnavailable(message="target-lock reap verdict failed") from exc
        return ReapVerdict(reap_eligible=eligible, reason=reason)

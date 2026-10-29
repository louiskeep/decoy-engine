"""Test doubles for the scratch subsystem.

These live in tests/ only and are never shipped as a production backend (plan
Slice 1 gate: no permissive fallback backend). ``QualifiedTmpfsProvider``
subclasses the real ``KernelProjectQuotaProvider`` and overrides only the two
kernel-quota probes so arena creation, path safety, observation and reclamation
run their real code against an ordinary tmpdir on the dev box; the real kernel
EDQUOT enforcement (A02) is proven separately on the GCP quota host.

The fake authorities exercise the ``OwnershipAuthority`` seam and its two real
implementations (``PlatformLeaseAuthority`` / ``TargetLockAuthority``) without a
live platform.
"""

from __future__ import annotations

from pathlib import Path

from decoy_engine.scratch._authority import AuthorityUnavailable, ReapVerdict
from decoy_engine.scratch._local_provider import KernelProjectQuotaProvider
from decoy_engine.scratch._policy import AuthorityMode, LeaseIdentity, ScratchPolicy
from decoy_engine.scratch._reports import QualificationResult, QuotaObservation


class QualifiedTmpfsProvider(KernelProjectQuotaProvider):
    """A provider that behaves like the real one but treats any existing
    directory as a qualified backend. For dev-box logic tests only."""

    def qualify(self, root: Path) -> QualificationResult:
        ok = root.exists() and root.is_dir()
        reasons = () if ok else ("arena root is not an existing directory",)
        return QualificationResult(
            qualified=ok, profile=self.profile.value, reasons=reasons, encryption_verified=False
        )

    def verify_quota(self, arena: Path, policy: ScratchPolicy) -> QuotaObservation:
        # Skip the kernel project-id check (no project quota on a dev tmpdir);
        # the real verify_quota is exercised on the GCP quota host.
        return self.observe(arena, policy)


class FakeAuthority:
    """A direct ``OwnershipAuthority`` double with configurable answers."""

    def __init__(
        self,
        *,
        mode: AuthorityMode = AuthorityMode.TARGET_LOCK,
        generation: int = 7,
        current: bool = True,
        reap_eligible: bool = False,
        reap_reason: str = "lease still active",
        outage: bool = False,
    ) -> None:
        self._mode = mode
        self._generation = generation
        self._current = current
        self._reap_eligible = reap_eligible
        self._reap_reason = reap_reason
        self._outage = outage

    @property
    def mode(self) -> AuthorityMode:
        return self._mode

    def lease_for(self, job_id: str, attempt_id: str, local_stage_root: str) -> LeaseIdentity:
        if self._outage:
            raise AuthorityUnavailable(message="fake authority outage")
        return LeaseIdentity(
            job_id=job_id,
            attempt_id=attempt_id,
            lease_id=f"lease-{job_id}-{attempt_id}",
            generation=self._generation,
            mode=self._mode,
            local_stage_root=local_stage_root,
        )

    def verify(self, lease: LeaseIdentity) -> bool:
        if self._outage:
            raise AuthorityUnavailable(message="fake authority outage")
        return self._current

    def reap_verdict(self, lease: LeaseIdentity) -> ReapVerdict:
        if self._outage:
            raise AuthorityUnavailable(message="fake authority outage")
        return ReapVerdict(reap_eligible=self._reap_eligible, reason=self._reap_reason)


class FakePlatformClient:
    """A ``PlatformLeaseClient`` double."""

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail

    def claim(self, job_id: str, attempt_id: str) -> tuple[str, int]:
        if self._fail:
            raise RuntimeError("db down")
        return (f"host-lease-{job_id}", 3)

    def is_current(self, job_id: str, lease_id: str, generation: int) -> bool:
        return True

    def recover_verdict(self, job_id: str, lease_id: str, generation: int) -> tuple[bool, str]:
        return (True, "released")


class FakeTargetLockClient:
    """A ``TargetLockClient`` double."""

    def lease_tuple(self, job_id: str, attempt_id: str) -> tuple[str, int]:
        return (f"target-lock-{job_id}", 1)

    def holds_target_lock(self, job_id: str, lease_id: str) -> bool:
        return True

    def released_and_terminal(self, job_id: str, lease_id: str) -> tuple[bool, str]:
        return (False, "job not terminal")


def make_policy(tmp_root: Path, **overrides: object) -> ScratchPolicy:
    kwargs: dict = {
        "job_id": "job-A",
        "attempt_id": "attempt-1",
        "quota_bytes": 64 << 20,
        "quota_inodes": 4096,
        # The admitted stage root the way the worker handoff would supply it.
        "labels": {"local_stage_root": str(tmp_root)},
    }
    kwargs.update(overrides)
    return ScratchPolicy(**kwargs)  # type: ignore[arg-type]

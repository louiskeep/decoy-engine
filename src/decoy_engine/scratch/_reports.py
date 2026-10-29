"""Typed reports the scratch subsystem returns: qualification, quota failure,
cleanup outcome, and sweep outcome.

The reports carry only control-plane facts (ids, caps, kernel observations,
dispositions), never source values or user-derived filenames (plan 1.5). The
quota report separates the kernel-observed charged bytes from the diagnostic
logical length, because the kernel project quota is the enforcement authority
and a tree walk of logical lengths is a diagnostic only (plan 1.4, 1.5 item 1).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class Disposition(str, enum.Enum):
    """What actually happened to a resource's bytes, reported honestly.

    ``UNKNOWN`` and ``KEY_REMOVAL_PENDING`` exist so a report never collapses an
    ambiguous outcome into a false "securely erased" (plan 1.7). Ordinary unlink
    is not secure erasure on SSDs, snapshots or backups.
    """

    DELETED = "deleted"
    RETAINED = "retained"
    KEY_REMOVAL_PENDING = "key_removal_pending"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class QualificationResult:
    """Outcome of a storage-provider qualification probe.

    ``qualified`` False means the backend fails closed; ``reasons`` lists the
    missing capabilities (no project quota, no encryption policy, multi-device)
    without leaking host paths beyond the arena root under test.
    """

    qualified: bool
    profile: str
    reasons: tuple[str, ...] = ()
    # Whether the arena root's at-rest encryption policy was verified. Slice 1
    # leaves this False and does not enforce it; Slice 4 requires it True before
    # any production write (plan Slice 4 gate; no managed-plaintext production).
    encryption_verified: bool = False


@dataclass(frozen=True, slots=True)
class QuotaObservation:
    """A point-in-time kernel observation plus a separate diagnostic length.

    ``bytes_used`` / ``inodes_used`` come from the kernel quota when the reader
    has the privilege to query it; otherwise they are None and ``observed_via``
    says so. ``logical_bytes`` is a diagnostic tree walk and is never used as the
    enforcement authority (plan 1.5 item 1).
    """

    bytes_limit: int
    inodes_limit: int
    bytes_used: int | None
    inodes_used: int | None
    logical_bytes: int
    observed_via: str


@dataclass(frozen=True, slots=True)
class QuotaReport:
    """The typed report attached to a ``scratch_quota_exceeded`` failure.

    Identifies the effective cap, observed usage, the arena/resource ids and the
    cleanup disposition, with no data values (plan 1.5 item 5, A02).
    """

    arena_id: str
    resource_id: str
    kind: str
    effective_bytes_cap: int
    effective_inodes_cap: int
    observation: QuotaObservation
    dimension: str  # "bytes", "inodes" or "unknown" (unknown-I/O classification)
    disposition: Disposition
    errno: int | None = None
    # How the failure was classified (plan 1.5 item 5): "errno" when the kernel
    # errno was preserved, "provider_quota" when a bounded kernel probe confirmed
    # the hard cap for an errno-less provider failure (DuckDB/COPY), or
    # "unknown_io" when a write failed with neither errno nor quota evidence, so a
    # quota breach is not claimed. A native exception string is never parsed.
    evidence: str = "errno"


@dataclass(frozen=True, slots=True)
class CleanupReport:
    """The outcome of releasing one resource.

    ``retained_bytes`` is the charge still held when cleanup could not confirm
    reclamation; the report never reports zero just because a pathname
    disappeared (plan 1.5 item 6).
    """

    arena_id: str
    resource_id: str
    disposition: Disposition
    retained_bytes: int = 0
    retained_inodes: int = 0
    detail: str = ""


@dataclass(frozen=True, slots=True)
class SweptResource:
    """One resource's sweep decision and reason."""

    arena_id: str
    resource_id: str
    disposition: Disposition
    reason: str


@dataclass(frozen=True, slots=True)
class SweepReport:
    """The outcome of a bounded sweep: what was retained, what was reaped, and
    what is left for reconciliation. Unknown entries are reported, never deleted
    by prefix (plan 1.8 step 1)."""

    scope: str
    resources: tuple[SweptResource, ...] = field(default_factory=tuple)

    @property
    def retained(self) -> tuple[SweptResource, ...]:
        return tuple(r for r in self.resources if r.disposition is Disposition.RETAINED)

    @property
    def reaped(self) -> tuple[SweptResource, ...]:
        return tuple(r for r in self.resources if r.disposition is Disposition.DELETED)

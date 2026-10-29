"""The storage-provider seam.

Policy is separated from storage capability because Linux provisioning and the
platform lease authority have different owners (plan 1.5 design notes). The
provider is the only place that touches the filesystem: it qualifies a root,
creates restrictive arenas, verifies the kernel project quota the deployment
adapter assigned, observes usage and reclaims space. The engine runs
unprivileged; the provider never acquires root or mounts volumes (plan 1.5,
Slice-0 report condition 1).

A provider implementation that cannot prove the hard boundary must fail its
``qualify`` call. There is deliberately no permissive in-tree provider that
skips the quota: an unsupported host is refused, not silently served from /tmp.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from decoy_engine.scratch._policy import ScratchPolicy
from decoy_engine.scratch._reports import QualificationResult, QuotaObservation


@runtime_checkable
class StorageProvider(Protocol):
    """Filesystem adapter under the ScratchManager.

    Implementations bind to one qualified profile. The reference implementation
    is ``KernelProjectQuotaProvider`` (ext4 + project quota; encryption in
    Slice 4). Tests may inject a double, but a double is never shipped as a
    production backend.
    """

    def qualify(self, root: Path) -> QualificationResult:
        """Probe ``root`` and return whether the hard boundary holds: project
        quota present and enforced, single-device placement, usable arena root.
        Never raises for an unqualified backend; it reports it."""
        ...

    def create_arena(self, root: Path, arena_id: str) -> Path:
        """Create a private 0700 child arena beneath the verified ``root`` with
        safe path resolution (no symlink, no traversal, owner/mode/device
        verified). The child inherits the project id. Raises ``ScratchPathUnsafe``
        on any safety violation (plan 1.5, A01)."""
        ...

    def verify_quota(self, arena: Path, policy: ScratchPolicy) -> QuotaObservation:
        """Verify the kernel project quota on ``arena`` matches ``policy`` and
        return an observation. Raises ``ScratchBackendUnavailable`` if the quota
        the deployment adapter should have assigned is absent or below policy."""
        ...

    def observe(self, arena: Path, policy: ScratchPolicy) -> QuotaObservation:
        """Return a current usage observation (kernel if permitted, otherwise a
        diagnostic logical length), for a quota report. Never the enforcement
        authority (plan 1.5 item 1)."""
        ...

    def reclaim(self, arena: Path) -> tuple[bool, int, int]:
        """Delete the arena subtree and confirm reclamation. Returns
        ``(confirmed, retained_bytes, retained_inodes)``. A failed cleanup keeps
        its charge (plan 1.5 item 6)."""
        ...

    def probe_exhaustion(self, arena: Path, policy: ScratchPolicy) -> tuple[bool, str]:
        """Authoritatively test whether ``arena`` is at its kernel hard cap, for a
        provider failure that lost its errno (DuckDB/COPY; plan 1.5 item 5).

        Attempt one bounded allocation under the arena and read the kernel's
        answer: ``EDQUOT``/``ENOSPC`` on inode creation is inode exhaustion, on a
        one-block write is byte exhaustion. Returns ``(exhausted, dimension)`` with
        ``dimension`` one of ``"inodes"`` / ``"bytes"`` (empty when not exhausted).
        This is the enforcement path, not the diagnostic logical walk, and it never
        parses an exception string. The probe leaves no residue."""
        ...

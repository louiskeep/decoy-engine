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

from decoy_engine.scratch._capabilities import Identity, VerifiedChild, VerifiedDir
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

    def qualify(self, root: VerifiedDir) -> QualificationResult:
        """Probe the already-opened, identity-verified ``root`` descriptor and
        return whether the hard boundary holds: project quota present and
        enforced, single-device placement, usable arena root. Reads the inode's
        fsxattr and mount options from ``root.fd``, never by re-resolving a path.
        Never raises for an unqualified backend; it reports it."""
        ...

    def create_arena(self, root: VerifiedDir, arena_id: str) -> tuple[Path, Identity]:
        """Create a private 0700 child arena beneath the verified ``root`` fd with
        ``os.mkdir(..., dir_fd=root.fd)`` and ``os.fchmod`` on the child fd (never
        a path ``chmod``/``stat``). The child inherits the project id. Returns the
        child path (diagnostic) and its pinned ``(st_dev, st_ino)``. Raises
        ``ScratchPathUnsafe`` on any safety violation (plan 1.5, A01)."""
        ...

    def verify_quota(self, arena: VerifiedDir, policy: ScratchPolicy) -> QuotaObservation:
        """Verify the kernel project quota on the verified ``arena`` fd matches
        ``policy`` and return an observation. Raises ``ScratchBackendUnavailable``
        if the quota the deployment adapter should have assigned is absent or
        below policy."""
        ...

    def observe(self, arena: VerifiedDir, policy: ScratchPolicy) -> QuotaObservation:
        """Return a current usage observation (kernel if permitted, otherwise a
        diagnostic ``os.fwalk`` length on ``arena.fd``), for a quota report. Never
        the enforcement authority (plan 1.5 item 1)."""
        ...

    def reclaim(self, target: VerifiedChild) -> tuple[bool, int, int]:
        """Delete the verified ``target`` and confirm reclamation, bound to the
        pinned descriptors. Returns ``(confirmed, retained_bytes,
        retained_inodes)``; a failed cleanup keeps its charge (plan 1.5 item 6).

        The delete is FD-relative: a file target is ``os.unlink(target.name,
        dir_fd=target.parent_fd)``; a directory target is torn down with
        ``dir_fd``-relative recursion. Reclamation is confirmed by an absence
        check under the pinned parent fd. There is no path string to re-resolve
        between the identity check and the delete, so a post-check symlink swap
        cannot redirect it outside the quota domain."""
        ...

    def probe_exhaustion(self, arena: VerifiedDir, policy: ScratchPolicy) -> tuple[bool, str]:
        """Authoritatively test whether the verified ``arena`` fd is at its kernel
        hard cap, for a provider failure that lost its errno (DuckDB/COPY; plan
        1.5 item 5). The probe file is created and unlinked ``dir_fd=arena.fd``.

        Attempt one bounded allocation under the arena and read the kernel's
        answer: ``EDQUOT``/``ENOSPC`` on inode creation is inode exhaustion, on a
        one-block write is byte exhaustion. Returns ``(exhausted, dimension)`` with
        ``dimension`` one of ``"inodes"`` / ``"bytes"`` (empty when not exhausted).
        This is the enforcement path, not the diagnostic logical walk, and it never
        parses an exception string. The probe leaves no residue."""
        ...

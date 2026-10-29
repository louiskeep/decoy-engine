"""Engine-facing release coordinator for signed evidence (F33 slice S2).

Writes land in a private staging directory; the complete, signed release becomes visible with one
atomic rename, or not at all. The default release evaluator denies everything until the engine
records a complete check ledger (S6). See ``docs/security/signed-evidence.md``.
"""

from __future__ import annotations

from ._staging import (
    ArtifactSpec,
    PublishedRelease,
    RejectedEvidence,
    ReleaseConflict,
    ReleaseDenied,
    ReleaseError,
    Stage,
    StagingArea,
    StagingCoordinator,
)

__all__ = [
    "ArtifactSpec",
    "PublishedRelease",
    "RejectedEvidence",
    "ReleaseConflict",
    "ReleaseDenied",
    "ReleaseError",
    "Stage",
    "StagingArea",
    "StagingCoordinator",
]

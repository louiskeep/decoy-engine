"""engine-v2 S10 post-execution validation (opt-in via `post_validation: true`).

Public API:

    from decoy_engine.validation.post import (
        PostValidationRunner,
        QualitySummary,
        DistinctCount,
        NullCount,
        FkValidityReport,
        CompositeCoherenceReport,
    )

The `PostValidationRunner` walks the post-execution scan suite over the masked S9
output and produces the `quality_summary` manifest block. Scaffolding + the flag
gate ship in slice 2; the 8 scans + the manifest forward land in slices 3-5.
"""

from __future__ import annotations

from decoy_engine.validation.post._runner import PostValidationRunner
from decoy_engine.validation.post._types import (
    CompositeCoherenceReport,
    DistinctCount,
    FkValidityReport,
    NullCount,
    QualitySummary,
)

__all__ = [
    "CompositeCoherenceReport",
    "DistinctCount",
    "FkValidityReport",
    "NullCount",
    "PostValidationRunner",
    "QualitySummary",
]

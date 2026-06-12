"""Statistical synthesis: sample columns from a distribution snapshot.

Capability-gaps WS3 (2026-06-12). The fitted-model artifact is the
EXISTING `distribution-snapshot/v1` (quality/snapshot.py) -- fitting is
`compute_distribution_snapshot`, exposed to operators as `decoy fit`.
This package consumes it: `load_spec` validates one statistical generate
column against the snapshot, `sample_column` draws deterministic
synthetic values from it.
"""

from decoy_engine.generation.statistical._sample import sample_column
from decoy_engine.generation.statistical._spec import (
    StatisticalSpec,
    StatisticalSpecError,
    load_spec,
)

__all__ = [
    "StatisticalSpec",
    "StatisticalSpecError",
    "load_spec",
    "sample_column",
]

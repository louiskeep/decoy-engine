"""Statistical synthesis: sample columns from a distribution snapshot.

Capability-gaps WS3 (2026-06-12). The fitted-model artifact is the
EXISTING `distribution-snapshot/v1` (quality/snapshot.py) -- fitting is
`compute_distribution_snapshot`, exposed to operators as `decoy fit`.
This package consumes it: `load_spec` validates one statistical generate
column against the snapshot, `sample_column` draws deterministic
synthetic values from it.
"""

from decoy_engine.generation.statistical._fanout import (
    FanoutSpec,
    add_fanout,
    fanout_spec_from_snapshot,
    fitted_reference_values,
)
from decoy_engine.generation.statistical._sample import sample_column
from decoy_engine.generation.statistical._spec import (
    StatisticalSpec,
    StatisticalSpecError,
    load_spec,
    spec_from_dict,
    spec_to_dict,
)

__all__ = [
    "FanoutSpec",
    "StatisticalSpec",
    "StatisticalSpecError",
    "add_fanout",
    "fanout_spec_from_snapshot",
    "fitted_reference_values",
    "load_spec",
    "sample_column",
    "spec_from_dict",
    "spec_to_dict",
]

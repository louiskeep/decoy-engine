"""Statistical synthesis: sample columns from a distribution snapshot.

Capability-gaps WS3 (2026-06-12). The fitted-model artifact is the
EXISTING `distribution-snapshot/v1` (quality/snapshot.py) -- fitting is
`compute_distribution_snapshot`, exposed to operators as `decoy fit`.
This package consumes it: `load_spec` validates one statistical generate
column against the snapshot, `sample_column` draws deterministic
synthetic values from it.
"""

from decoy_engine.generation.statistical._cell_format import (
    CellFormat,
    add_cell_format,
    cell_format_arrow_type,
    cell_format_member_ok,
    learn_cell_format,
    scrub_cell_format,
)
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
    "CellFormat",
    "FanoutSpec",
    "StatisticalSpec",
    "StatisticalSpecError",
    "add_cell_format",
    "add_fanout",
    "cell_format_arrow_type",
    "cell_format_member_ok",
    "fanout_spec_from_snapshot",
    "fitted_reference_values",
    "learn_cell_format",
    "load_spec",
    "sample_column",
    "scrub_cell_format",
    "spec_from_dict",
    "spec_to_dict",
]

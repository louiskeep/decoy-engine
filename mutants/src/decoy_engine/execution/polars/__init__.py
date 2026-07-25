"""engine-v2 S11 polars execution substrate.

The second `ExecutionAdapter` implementation plus the polars-direct I/O boundary.
At S11 close the adapter masks via the pandas oracle (no strategy is
polars-native yet); S12 migrates the 11 strategies. See
`docs/v2/sprints/engine-v2/sprint-11-execution-adapter-polars-boundary.md`.

Public API:

    from decoy_engine.execution.polars import (
        PolarsExecutionAdapter,
        read_source_polars,
        write_target_polars,
        ConversionBoundary,
    )
"""

from __future__ import annotations

from decoy_engine.execution.polars._conversion_boundary import ConversionBoundary
from decoy_engine.execution.polars._polars_adapter import PolarsExecutionAdapter
from decoy_engine.execution.polars._source_reader import read_source_polars
from decoy_engine.execution.polars._target_writer import write_target_polars

__all__ = [
    "ConversionBoundary",
    "PolarsExecutionAdapter",
    "read_source_polars",
    "write_target_polars",
]

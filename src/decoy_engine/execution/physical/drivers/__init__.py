"""The six delegating driver adapters (Task 4.2, D2).

Each adapter here forwards its call, verbatim, to the exact production entry
point the design doc names for that driver and returns the delegate's result
UNCHANGED -- no re-aggregation, no field mutation, no fallback or publication
ownership. See each module's docstring for the driver-specific delegation
shape and `tests/physical/test_lossless_forwarding.py` for the sentinel proof.

Nothing in `decoy_engine.execution` (outside this package) imports from here;
nothing here is imported by `run_pipeline` or any current route/coordinator/
executor module (enforced by `tests/sentry/test_physical_seam_disconnection.py`).
"""

from __future__ import annotations

from decoy_engine.execution.physical.drivers._chunked import (
    MaskPipelineChunkedAdapter,
    NativeOrOracleChunkedAdapter,
    ResidentChunkedAggregatorAdapter,
)
from decoy_engine.execution.physical.drivers._full_frame import FullFrameAdapter
from decoy_engine.execution.physical.drivers._native_stream import NativeStreamAdapter
from decoy_engine.execution.physical.drivers._out_of_core import OutOfCoreAdapter
from decoy_engine.execution.physical.drivers._sequential import SequentialAdapter
from decoy_engine.execution.physical.drivers._synthesis import SynthesisStageAdapter

__all__ = [
    "FullFrameAdapter",
    "MaskPipelineChunkedAdapter",
    "NativeOrOracleChunkedAdapter",
    "NativeStreamAdapter",
    "OutOfCoreAdapter",
    "ResidentChunkedAggregatorAdapter",
    "SequentialAdapter",
    "SynthesisStageAdapter",
]

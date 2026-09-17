"""Source-shaped output assembly for the unified slice (D9 peak-RSS fix).

Rebuilds the source-shaped pandas frame from the Arrow source AFTER the shadow
coordinator returns, overlays the coordinator's masked columns positionally, and
converts back to Arrow. Split out of `_unified_slice.py` for two reasons:

- The full-source pandas copy is built PAST the coordinator's native-execution
  peak, so it never coexists with the coordinator's Rust-allocated (FFI-exported,
  pyarrow-invisible) working set. Holding that copy across the native run was the
  D9 1M peak-RSS overshoot; `frame` and `masked_table` are function-local here and
  release on return.
- It keeps `_unified_slice.py` under the 600-LOC orchestration cap.

Seam discipline: this module imports NOTHING from `decoy_engine.execution.physical`.
The physical-plan and coordinator-result shapes are annotated structurally via
`Protocol`, so the module stays on the non-physical side of the Task 4.5 seam. See
`tests/sentry/test_physical_seam_disconnection.py` (its import sweep is a textual
check, so even a type-only import of a physical name would trip it).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

import pyarrow as pa

from decoy_engine.execution._unified_slice_admission import CheapCandidate


class _MaskNode(Protocol):
    @property
    def strategy(self) -> str: ...

    @property
    def columns(self) -> Sequence[str]: ...


class _PhysicalTableLike(Protocol):
    @property
    def nodes(self) -> Sequence[_MaskNode]: ...


class _ShadowResultLike(Protocol):
    @property
    def outputs(self) -> dict[str, pa.Table]: ...


def source_shaped_output(
    candidate: CheapCandidate,
    shadow_result: _ShadowResultLike,
    physical_table: _PhysicalTableLike,
) -> dict[str, pa.Table]:
    """Assemble the lane's Arrow output in the shape of `candidate.source`.

    The frame is the SAME source-aware pandas conversion the legacy adapter
    performs (`_pandas_adapter.py`'s `to_pandas_fk_safe` reduces to a plain
    `to_pandas()` here, since cheap admission already declined any
    relationship-bearing job). Leaving a passthrough column untouched reproduces
    the legacy `PassthroughHandler` no-op; overlaying a masked column's
    `to_pylist()` positionally reproduces every tokenizing handler's own
    `df[column] = masked.to_pylist()`; and the closing
    `pa.Table.from_pandas(frame, preserve_index=False)` is the legacy adapter's
    own conversion, attaching the identical `b"pandas"` schema metadata by
    construction.

    `candidate.source_frame` used to be prebuilt at admission and carried here.
    It is now rebuilt from `candidate.source` at call time: admission proved this
    exact conversion succeeds and round-trips type-/value-clean before admitting,
    so the rebuild is byte-identical to the frame admission validated, and
    building it here keeps it out of the coordinator's peak.

    MUST be called only after the coordinator run returns (see the ordering test
    in `tests/physical/test_unified_slice_reconstruct.py`): building the frame
    before or during native execution would stay correct but reintroduce the
    peak-RSS overshoot.
    """
    frame = candidate.source.to_pandas()
    # Take ownership of the coordinator's masked table and release it before the
    # closing `from_pandas`. `pop` (not index) drops the last reference so `del`
    # frees it; the coordinator result is not read for its outputs again.
    masked_table = shadow_result.outputs.pop(candidate.table)
    for node in physical_table.nodes:
        if node.strategy == "passthrough":
            continue
        column = node.columns[0]
        frame[column] = masked_table.column(column).to_pylist()
    del masked_table
    return {candidate.table: pa.Table.from_pandas(frame, preserve_index=False)}

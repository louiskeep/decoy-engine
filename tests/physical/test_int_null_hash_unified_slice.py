"""Unified-slice lane over an integer hash column that holds nulls.

A mixed-null column reconstructs as double in the lane's own pandas round trip, so admission
declines it and the legacy route (which keys from the exact integers) produces the output. An
all-null column cannot diverge and is admitted natively, giving all-null output. Either way the
flag-on call must agree cell for cell with the flag-off call.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest

from decoy_engine.execution._unified_slice import QUALITY_METRICS_KEY
from decoy_engine.execution.native._companion_status import native_companion_status
from tests.physical.test_unified_slice_parity import _assert_outputs_cell_identical, _run_both

_HASH = [{"name": "c", "strategy": "hash", "namespace": "n"}]
_VALUES = {
    "int64": ([-(2**63), 1, None, 2**63 - 1], pa.int64()),
    "uint64-max": ([0, 2**64 - 1, None, 2**63], pa.uint64()),
    "int8": ([-128, None, 127, 0], pa.int8()),
}


@pytest.mark.parametrize("case", list(_VALUES))
def test_mixed_null_integer_hash_declines_the_lane_and_matches_legacy(
    tmp_path: Path, case: str
) -> None:
    values, arrow_type = _VALUES[case]
    source = pa.table({"c": pa.array(values, arrow_type)})
    off, on = _run_both(tmp_path, "t", source, _HASH)
    _assert_outputs_cell_identical(off, on)
    out = on.outputs["t"].column("c").to_pylist()
    assert [v is None for v in out] == [v is None for v in values]
    assert all(isinstance(v, str) and len(v) == 64 for v in out if v is not None)
    # Declined: the lane stamps no completed-execution evidence for this table.
    assert QUALITY_METRICS_KEY not in on.quality_metrics


@pytest.mark.skipif(
    not native_companion_status().ok, reason="compiled decoy-engine-native companion unavailable"
)
def test_all_null_integer_hash_is_admitted_and_yields_all_null(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array([None, None, None], pa.int64())})
    off, on = _run_both(tmp_path, "t", source, _HASH)
    _assert_outputs_cell_identical(off, on)
    assert on.outputs["t"].column("c").to_pylist() == [None, None, None]
    assert QUALITY_METRICS_KEY in on.quality_metrics

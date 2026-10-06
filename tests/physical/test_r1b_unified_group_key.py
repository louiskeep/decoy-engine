"""R1b baseline: a group_key job activates the unified route end to end.

group_key is the one operator that reads a column other than its target, and the unified
admission's resident-types check reads that sibling name off the node's binding before any
operator runs. A direct `run_operator` comparison cannot see a reader that stops finding the
name, so this runs the whole lane: the legacy adapter is poisoned (a decline would raise), the
output is compared with a separate lane-off run, and the admission check is observed
judging the sibling's own type.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution.native import _operator_config_rejections
from decoy_engine.execution.native._chunked_evidence import RUST_COMPANION
from tests.physical.test_unified_route_evidence import (
    NEEDS_COMPANION,
    OPERATORS,
    _evidence_for,
    assert_exact_evidence,
    lane_nodes,
)


@NEEDS_COMPANION
def test_group_key_job_runs_on_the_unified_route_and_reads_the_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    columns = OPERATORS["native_group_key"][0]
    # An int64 sibling next to a string target: the admission check can only see int64 by
    # reading the sibling's name, never the target's.
    source = pa.table(
        {
            "gb": pa.array([1, 2, 1, 2, 3], type=pa.int64()),
            "gk": pa.array(["seed"] * 5, type=pa.string()),
        }
    )
    judged: list[pa.DataType] = []
    real = _operator_config_rejections.group_key_sibling_type_admitted

    def spy(arrow_type: pa.DataType) -> bool:
        judged.append(arrow_type)
        return real(arrow_type)

    monkeypatch.setattr(_operator_config_rejections, "group_key_sibling_type_admitted", spy)

    nodes: dict[str, dict[str, Any]] = lane_nodes(tmp_path, source, columns, monkeypatch)

    assert_exact_evidence(
        _evidence_for(nodes, "native_group_key"),
        operator="native_group_key",
        compiled=True,
        planned=RUST_COMPANION,
        executed=RUST_COMPANION,
        calls=1,
    )
    assert pa.int64() in judged

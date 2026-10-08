"""C8-iii-d-2 acceptance: positional categorical / REUSE faker under `when:` on the
unified full-frame route, byte-identical to the d-1 lane-off oracle.

Plan: `docs/plans/2026-10-08-c8-iii-d2-native-positional-when.md` rev 3, section 3. The
lane-off run (`unified_slice_enabled=False`) is the d-1 full-frame oracle; the lane-on run
poisons `PandasExecutionAdapter.run`, so a silent reroute cannot pass. Every admitted case
asserts the native kernel ran (`compiled_kernel_executed`).

Covers tests 1, 1a (unified leg), 5 and 6 for the unified route.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution._unified_slice import QUALITY_METRICS_KEY
from tests.physical.test_unified_slice_faker import NEEDS_COMPANION, Case
from tests.physical.test_unified_slice_positional import (
    CATEGORICAL_OP,
    FAKER_OP,
    cat_col,
    nd_faker,
    node_evidence,
    parity,
)

POS_BUILDERS = [
    pytest.param(lambda **kw: cat_col(**kw), CATEGORICAL_OP, id="categorical"),
    pytest.param(
        lambda **kw: cat_col(weighted=True, **kw), CATEGORICAL_OP, id="categorical_weighted"
    ),
    pytest.param(lambda **kw: nd_faker(**kw), FAKER_OP, id="faker"),
]


def two_col(
    n: int, keep: Callable[[int], str], *, null_at: Callable[[int], bool] | None = None
) -> pa.Table:
    c = [None if null_at and null_at(i) else f"src_{i % 5}" for i in range(n)]
    return pa.table(
        {
            "c": pa.array(c, pa.string()),
            "keep": pa.array([keep(i) for i in range(n)], pa.string()),
        }
    )


def when_on_c(predicate: str) -> Callable[[dict[str, Any]], None]:
    def mutate(config: dict[str, Any]) -> None:
        config["tables"][0]["columns"][0]["when"] = predicate

    return mutate


_MATRIX = [
    ("none", "keep == 'NOPE'", lambda i: "k", False),
    ("all", "keep != 'NOPE'", lambda i: "k", True),
    ("every_other", "keep == 'A'", lambda i: "A" if i % 2 == 0 else "B", True),
    ("self_reference", "c == 'src_1'", lambda i: "k", True),
]


# ---------------------------------------------------------------------------
# 1. Unified parity against an explicit lane-off oracle, with native evidence.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
@pytest.mark.parametrize("build, operator", POS_BUILDERS)
@pytest.mark.parametrize("which, predicate, keep, nonempty", _MATRIX)
def test_1_unified_is_byte_identical_to_the_lane_off_oracle(
    tmp_path: Path,
    build: Any,
    operator: str,
    which: str,
    predicate: str,
    keep: Callable[[int], str],
    nonempty: bool,
) -> None:
    source = two_col(37, keep, null_at=lambda i: i % 7 == 3)
    columns = [build(), {"name": "keep", "strategy": "passthrough"}]
    case = Case(tmp_path, source, columns, mutate=when_on_c(predicate))
    leaf = parity(case, batch=8)
    # A nonempty selection must have run the compiled kernel; a none-selected `when:` node is the
    # masked-node zero-selection exemption, so it executes without a compiled kernel.
    assert node_evidence(leaf, operator)["compiled_kernel_executed"] is nonempty


# ---------------------------------------------------------------------------
# 1a. Reference chunk-stability on the unified route.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
@pytest.mark.parametrize("build", [cat_col, nd_faker])
def test_1a_numeric_reference_falls_to_the_full_frame_oracle(tmp_path: Path, build: Any) -> None:
    # The unified route keeps whole-frame predicate evaluation, so a numeric reference is safe
    # on the ORACLE; the shared native gate still declines it, so the whole table runs lane-off.
    source = pa.table(
        {
            "c": pa.array([f"src_{i % 4}" for i in range(16)], pa.string()),
            "p": pa.array([9007199254740992 + (i % 3) for i in range(16)], pa.int64()),
        }
    )
    columns = [build(), {"name": "p", "strategy": "passthrough"}]
    case = Case(tmp_path, source, columns, mutate=when_on_c("p == 9007199254740992"))
    off = case.run(lane=False)
    on = case.run(lane=True)
    assert QUALITY_METRICS_KEY not in on.quality_metrics  # the lane did not activate
    assert on.outputs["t"].equals(off.outputs["t"], check_metadata=True)


@NEEDS_COMPANION
@pytest.mark.parametrize("build, operator", POS_BUILDERS)
def test_1a_string_reference_runs_natively(tmp_path: Path, build: Any, operator: str) -> None:
    source = two_col(24, lambda i: "A" if i % 2 else "B")
    columns = [build(), {"name": "keep", "strategy": "passthrough"}]
    case = Case(tmp_path, source, columns, mutate=when_on_c("keep == 'A'"))
    leaf = parity(case, batch=7)
    assert node_evidence(leaf, operator)["compiled_kernel_executed"] is True

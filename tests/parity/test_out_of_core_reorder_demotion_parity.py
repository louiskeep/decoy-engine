"""T5 (`docs/plans/2026-10-15-reorder-parent-size-guard.md` section 5): a
reorder-eligible sink job whose parent is above the parent-size guard's ceiling
demotes to the batch lane and emits the same rows in the same order as an actual
reorder run and as the pandas oracle.

Uses the route-seam parity harness shape (`test_out_of_core_route_seam_parity.py`):
a transactional sink, resolved budgets, and constructor spies. The plain
`test_out_of_core_fk_parity.py` helper supplies no sink or budget, so it would
bypass reorder admission entirely and prove nothing here.

Comparison is positional on the Arrow tables the sink wrote (exact schema +
metadata against the reorder control), and value-equal against the oracle under
that suite's documented normalizations. It is not a claim of serialized-Parquet
byte identity.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from decoy_engine.execution import ParquetTransactionalSink
from decoy_engine.execution.out_of_core import run_fk_out_of_core
from decoy_engine.execution.out_of_core._batch_join import ChildFkBatchJoiner
from decoy_engine.execution.out_of_core._reorder_budget import (
    resolve_reorder_budgets,
    safe_parent_key_ceiling,
)
from decoy_engine.execution.out_of_core._stream_join import StreamFkJoiner
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity.test_out_of_core_fk_parity import (
    _REG,
    _assert_value_equal,
    _build_single_edge,
    _run_oracle,
)
from tests.parity.test_out_of_core_route_seam_parity import _assert_tables_exactly_equal

_MIB = 1024 * 1024
_DISK = 200 * 1024**3
# 128 MiB puts the ceiling at 466_666 keys (see `_reorder_budget`), so a 500k-key
# parent is genuinely above it without a patched constant.
_SMALL_BUDGET = 128 * _MIB
_PARENT_KEYS = 500_000
# 1 GiB puts the ceiling at ~3.7M keys: the same fixture is below it and reorders.
_LARGE_BUDGET = 1024 * _MIB


def _run(
    plan: Any, sources: Any, graph: Any, target: Path, *, budget: int
) -> tuple[Any, dict[str, bool]]:
    saw = {"batch": False, "reorder": False}
    orig_batch, orig_reorder = ChildFkBatchJoiner.__init__, StreamFkJoiner.__init__

    def spy_batch(self: Any, *a: Any, **k: Any) -> None:
        saw["batch"] = True
        orig_batch(self, *a, **k)

    def spy_reorder(self: Any, *a: Any, **k: Any) -> None:
        saw["reorder"] = True
        orig_reorder(self, *a, **k)

    ChildFkBatchJoiner.__init__ = spy_batch  # type: ignore[method-assign]
    StreamFkJoiner.__init__ = spy_reorder  # type: ignore[method-assign]
    try:
        result = run_fk_out_of_core(
            plan,
            sources,
            registry=_REG,
            relationship_graph=graph,
            sink=ParquetTransactionalSink(target),
            temp_dir=target.parent / f"{target.name}-work",
            budget_bytes=budget,
            temp_disk_budget_bytes=_DISK,
            out_of_core_reorder_threshold_rows=0,
        )
    finally:
        ChildFkBatchJoiner.__init__ = orig_batch  # type: ignore[method-assign]
        StreamFkJoiner.__init__ = orig_reorder  # type: ignore[method-assign]
    return result, saw


def test_t5_demoted_parent_matches_reorder_control_and_oracle(tmp_path: Path) -> None:
    refs: list[int | None] = [i % _PARENT_KEYS for i in range(_PARENT_KEYS)]
    refs[7] = -1  # an orphan
    refs[11] = None  # a null FK
    plan, sources, graph = _build_single_edge(
        parent_kind="str",
        child_kind="str",
        strategy="hash",
        policy=OrphanPolicy.PRESERVE,
        parent_rows=_PARENT_KEYS,
        child_refs=refs,
        parent_nan_row=None,
    )
    # The fixture is above the small budget's ceiling and below the large one's.
    small = safe_parent_key_ceiling(resolve_reorder_budgets(_SMALL_BUDGET, _DISK))
    large = safe_parent_key_ceiling(resolve_reorder_budgets(_LARGE_BUDGET, _DISK))
    assert small < _PARENT_KEYS < large

    demoted_dir, control_dir = tmp_path / "demoted", tmp_path / "control"
    _, demoted_saw = _run(plan, sources, graph, demoted_dir, budget=_SMALL_BUDGET)
    _, control_saw = _run(plan, sources, graph, control_dir, budget=_LARGE_BUDGET)

    # Witness: the demoted run built the batch joiner and never the reorder one;
    # the control is a real reorder run, so the comparison is not batch-vs-batch.
    assert demoted_saw["batch"] and not demoted_saw["reorder"]
    assert control_saw["reorder"] and not control_saw["batch"]

    oracle = _run_oracle(plan, sources, graph)
    for name in ("parent", "child"):
        demoted = pq.read_table(demoted_dir / f"{name}.parquet")
        control = pq.read_table(control_dir / f"{name}.parquet")
        _assert_tables_exactly_equal(demoted, control, f"demoted-vs-reorder:{name}")
        _assert_value_equal(oracle.outputs[name], demoted, f"demoted-vs-oracle:{name}")

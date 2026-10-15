"""Reorder-lane parent-size admission guard
(`docs/plans/2026-10-15-reorder-parent-size-guard.md` section 5, T1-T4).

`decide_route` demotes a reorder-eligible table to the bounded batch lane when
its largest incoming parent-key count exceeds `safe_parent_key_ceiling`. The
guard is a cardinality backstop calibrated against three recorded anchors; the
anchor tests here pin all three ends of the calibration band so a drift in
`_REORDER_BUILD_BYTES_PER_KEY` fails loudly instead of silently demoting a
recorded moderate case or admitting the 100M-class parent.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution.out_of_core import _route_policy
from decoy_engine.execution.out_of_core._memory_estimate import actual_duckdb_cap_bytes
from decoy_engine.execution.out_of_core._relation import ParentKeyRelation
from decoy_engine.execution.out_of_core._reorder_budget import (
    _REORDER_BUILD_BYTES_PER_KEY,
    resolve_reorder_budgets,
    safe_parent_key_ceiling,
)
from decoy_engine.execution.out_of_core._route_policy import (
    REORDER_PARENT_KEY_THRESHOLD,
    RouteDecision,
    decide_route,
)
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge

_GIB = 1024**3
_DISK = 200 * _GIB
_SINK = object()  # decide_route only checks `is None`
_BATCH = RouteDecision(use_reorder=False, reorder_caps=None)
_EDGE = RelationshipEdge(
    parent_table="parent",
    parent_columns=("pk",),
    child_table="child",
    child_columns=("fk",),
    namespace="ns",
    orphan_policy=OrphanPolicy.PRESERVE,
)


def _relation(tmp_path: Path, n_rows: int) -> ParentKeyRelation:
    """A real parquet relation whose footer `num_rows` is the decision key."""
    path = tmp_path / f"rel{n_rows}.parquet"
    pq.write_table(pa.table({"__decoy_fk_join_key": pa.array(range(n_rows), pa.int64())}), path)
    return ParentKeyRelation(path=path)


def _decide(
    tmp_path: Path, keys: int, budget: int, *, threshold: int = REORDER_PARENT_KEY_THRESHOLD
) -> RouteDecision:
    return decide_route(
        (_EDGE,),
        {_EDGE: _relation(tmp_path, keys)},
        sink=_SINK,
        budget_bytes=budget,
        temp_disk_budget_bytes=_DISK,
        threshold_rows=threshold,
    )


def _ceiling(budget: int) -> int:
    return safe_parent_key_ceiling(resolve_reorder_budgets(budget, _DISK))


def test_t1_large_parent_demotes_to_batch(tmp_path: Path) -> None:
    ceiling = _ceiling(_GIB)
    assert _decide(tmp_path, ceiling * 2, _GIB) == _BATCH


def test_t2_boundary_is_strictly_greater_than(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ceiling-1 and ceiling reorder, ceiling+1 demotes, all above the 2M selection
    threshold. The count is injected so the test is exact without writing 3.7M-row
    files; the footer read itself is covered by the real-file tests in this module."""
    ceiling = _ceiling(_GIB)
    assert ceiling > REORDER_PARENT_KEY_THRESHOLD
    relation = _relation(tmp_path, 10)
    for keys, expect_reorder in [(ceiling - 1, True), (ceiling, True), (ceiling + 1, False)]:
        monkeypatch.setattr(_route_policy, "_parent_key_count", lambda _r, k=keys: k)
        decision = decide_route(
            (_EDGE,),
            {_EDGE: relation},
            sink=_SINK,
            budget_bytes=_GIB,
            temp_disk_budget_bytes=_DISK,
            threshold_rows=REORDER_PARENT_KEY_THRESHOLD,
        )
        assert decision.use_reorder is expect_reorder, keys
        if not expect_reorder:
            assert decision == _BATCH


def test_t2_ceiling_below_selection_threshold(tmp_path: Path) -> None:
    """At 128 MiB the ceiling (466_666) sits under the 2M selection threshold: the
    sub-threshold path already returns batch, and the guard must not change that."""
    assert _ceiling(128 * 1024**2) < REORDER_PARENT_KEY_THRESHOLD
    assert _decide(tmp_path, 1_000_000, 128 * 1024**2) == _BATCH


def test_t2_threshold_zero_still_guarded(tmp_path: Path) -> None:
    """threshold=0 means "reorder every eligible table"; it must not switch the guard
    off, and a parent at the ceiling must still reorder."""
    budget = 128 * 1024**2
    ceiling = _ceiling(budget)
    assert _decide(tmp_path, ceiling, budget, threshold=0).use_reorder is True
    assert _decide(tmp_path, ceiling + 1, budget, threshold=0) == _BATCH


def test_t3_recorded_anchors_pin_the_calibration_band(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The three anchors that bound `_REORDER_BUILD_BYTES_PER_KEY` to [120, 166]."""
    relation = _relation(tmp_path, 10)

    def route(keys: int, budget: int, threshold: int) -> RouteDecision:
        monkeypatch.setattr(_route_policy, "_parent_key_count", lambda _r: keys)
        return decide_route(
            (_EDGE,),
            {_EDGE: relation},
            sink=_SINK,
            budget_bytes=budget,
            temp_disk_budget_bytes=_DISK,
            threshold_rows=threshold,
        )

    # Recorded D4a: 2.2M parents COMPLETED on reorder at 1 GiB.
    assert route(2_200_000, _GIB, REORDER_PARENT_KEY_THRESHOLD).use_reorder is True
    # 100M proof: ~35M parents at 8 GB was the unbounded-overshoot case.
    assert route(35_000_000, 8_000_000_000, REORDER_PARENT_KEY_THRESHOLD) == _BATCH
    # test_d4a_reorder_reachability: 420k parents at 128 MiB, both children reorder.
    assert route(420_000, 128 * 1024**2, 50_000).use_reorder is True


@pytest.mark.parametrize(
    ("budget", "expected_cap"),
    [(8_000_000_000, 4_196_000_000), (_GIB, 563_000_000), (128 * 1024**2, 70_000_000)],
)
def test_t4_ceiling_uses_the_enforced_duckdb_cap(budget: int, expected_cap: int) -> None:
    budgets = resolve_reorder_budgets(budget, _DISK)
    # The enforced cap (decimal "MB" after a whole-MiB floor), not the nominal 0.55x.
    assert actual_duckdb_cap_bytes(budgets.duckdb_memory_limit_bytes, 1) == expected_cap
    assert safe_parent_key_ceiling(budgets) == expected_cap // _REORDER_BUILD_BYTES_PER_KEY


def test_t4_constant_sits_inside_the_calibrated_band() -> None:
    assert 120 <= _REORDER_BUILD_BYTES_PER_KEY <= 166
    assert _ceiling(8_000_000_000) == 27_973_333

"""`evaluate_capacity`: the pure evaluator R1 extracted out of
`enforce_ooc_memory_preflight`, shared by the mid-run gate and the
estimate-only `estimate_job_capacity` entrypoint.

T4/T5 (docs/plans/2026-07-24-oom-checker-cli-v1.md, "Revised acceptance
tests"): FIT/INSUFFICIENT/NOT_APPLICABLE on typed inputs, parity with the
mid-run raise on the SAME inputs, and the undetectable-budget UNKNOWN case.
Also covers the two extra branches the extraction introduced: an unresolved
(CSV-estimate) parent table forcing UNKNOWN, and the fan-in guard's
`out_of_core_fanin_exceeds_budget` converting to INSUFFICIENT while any OTHER
`ExecutionError` still propagates untouched (R3).
"""

from __future__ import annotations

import pytest

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.out_of_core import _memory_estimate as mem_mod
from decoy_engine.execution.out_of_core._memory_estimate import (
    CapacityInputs,
    CapacityVerdict,
    enforce_ooc_memory_preflight,
    evaluate_capacity,
    predict_ooc_build_floor_bytes,
)

_MIB = 1024 * 1024
_GIB = 1024 * _MIB


def test_fit_within_budget() -> None:
    inputs = CapacityInputs(
        route="out_of_core",
        parent_table_rows={"parent": 100},
        incoming_edge_counts={},
        sink=True,
    )
    est = evaluate_capacity(inputs, 64 * _GIB)
    assert est.verdict is CapacityVerdict.FIT
    assert est.code is None
    assert est.needed_bytes is not None
    assert est.available_bytes == 64 * _GIB


def test_not_applicable_for_non_ooc_route() -> None:
    inputs = CapacityInputs(
        route="full_frame",
        parent_table_rows={},
        incoming_edge_counts={},
        sink=False,
    )
    est = evaluate_capacity(inputs, 64 * _GIB)
    assert est.verdict is CapacityVerdict.NOT_APPLICABLE
    assert est.code is None
    assert est.route == "full_frame"


def test_unknown_when_budget_undetectable() -> None:
    inputs = CapacityInputs(
        route="out_of_core",
        parent_table_rows={"parent": 50_000_000},
        incoming_edge_counts={},
        sink=True,
    )
    est = evaluate_capacity(inputs, None)
    assert est.verdict is CapacityVerdict.UNKNOWN
    assert est.code is None
    assert est.available_bytes is None


def test_unknown_when_a_parent_table_is_unresolved() -> None:
    """A CSV parent table's row count is an estimate, never an exact count --
    R6: never gate a hard-fail on an approximation, so this must be UNKNOWN,
    not FIT or INSUFFICIENT, regardless of what the (never-consulted)
    `parent_table_rows` would otherwise say."""
    inputs = CapacityInputs(
        route="out_of_core",
        parent_table_rows={},
        incoming_edge_counts={},
        sink=True,
        unresolved_parent_tables=frozenset({"parent"}),
    )
    est = evaluate_capacity(inputs, 64 * _GIB)
    assert est.verdict is CapacityVerdict.UNKNOWN
    assert "parent" in est.message


class TestParityWithMidRunGate:
    """T4: `evaluate_capacity` INSUFFICIENT on typed inputs must match the
    mid-run gate's raise on the SAME inputs -- not just the same verdict, the
    identical code AND message, since `enforce_ooc_memory_preflight` now
    raises `ExecutionError(code=est.code, message=est.message)` verbatim."""

    def test_insufficient_matches_the_mid_run_raise(self) -> None:
        parent_table_rows = {"parent": 20_000_000}
        budget = 64 * _MIB
        inputs = CapacityInputs(
            route="out_of_core",
            parent_table_rows=parent_table_rows,
            incoming_edge_counts={},
            sink=True,
        )
        est = evaluate_capacity(inputs, budget)
        assert est.verdict is CapacityVerdict.INSUFFICIENT
        assert est.code == "out_of_core_insufficient_memory"

        with pytest.raises(ExecutionError) as excinfo:
            enforce_ooc_memory_preflight(
                parent_table_rows, budget_bytes=budget, sink=True, incoming_edge_counts={}
            )
        assert excinfo.value.code == est.code
        assert excinfo.value.message == est.message
        assert est.needed_bytes is not None
        assert est.needed_bytes % _GIB == 0  # declared_minimum_ceiling_bytes rounds to whole GiB

    def test_fanin_exceeds_budget_matches_the_mid_run_raise(self) -> None:
        # 68 co-live joiners on a 64 MiB budget: a genuine over-commit even at
        # DuckDB's 1 MB floor (68 * 1e6 > 67_108_864) -- the SAME pinned case
        # `test_out_of_core_memory_estimate.py` exercises against the mid-run gate.
        incoming_edge_counts = {"leaf": 68}
        budget = 64 * _MIB
        inputs = CapacityInputs(
            route="out_of_core",
            parent_table_rows={},
            incoming_edge_counts=incoming_edge_counts,
            sink=True,
        )
        est = evaluate_capacity(inputs, budget)
        assert est.verdict is CapacityVerdict.INSUFFICIENT
        assert est.code == "out_of_core_fanin_exceeds_budget"

        with pytest.raises(ExecutionError) as excinfo:
            enforce_ooc_memory_preflight(
                {}, budget_bytes=budget, sink=True, incoming_edge_counts=incoming_edge_counts
            )
        assert excinfo.value.code == est.code
        assert excinfo.value.message == est.message


def test_unexpected_execution_error_propagates_not_swallowed(monkeypatch) -> None:
    """R3: only `out_of_core_fanin_exceeds_budget` converts to INSUFFICIENT.
    Any OTHER `ExecutionError` raised while sizing a table's cap is a genuine
    defect (a caller-usage bug), and must propagate rather than becoming a
    false `UNKNOWN`/`FIT` verdict."""

    def _boom(_budget_bytes: int, _live: int) -> int:
        raise ExecutionError(code="some_other_defect", message="not a capacity refusal")

    monkeypatch.setattr(mem_mod, "actual_duckdb_cap_bytes", _boom)
    inputs = CapacityInputs(
        route="out_of_core",
        parent_table_rows={"parent": 100},
        incoming_edge_counts={},
        sink=True,
    )
    with pytest.raises(ExecutionError) as excinfo:
        evaluate_capacity(inputs, 64 * _GIB)
    assert excinfo.value.code == "some_other_defect"


def test_warn_band_still_reports_fit_with_a_warned_flag(caplog) -> None:
    """The pre-extraction WARN behavior (never blocks, logs an advisory) is
    preserved -- it just now lives on `CapacityEstimate.warned` /
    `enforce_ooc_memory_preflight`'s own logging, not a third verdict."""
    floor = predict_ooc_build_floor_bytes(100_000)
    cap = int(floor / 0.7)  # inside the [0.6, 1.0) warn band
    inputs = CapacityInputs(
        route="out_of_core",
        parent_table_rows={"parent": 100_000},
        incoming_edge_counts={},
        sink=True,
    )
    est = evaluate_capacity(inputs, cap)
    assert est.verdict is CapacityVerdict.FIT
    assert est.warned is True
    assert "resident floor" in est.message

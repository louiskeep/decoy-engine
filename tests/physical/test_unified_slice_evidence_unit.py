"""Pure unit tests for `assemble_node_evidence` (the evidence seam).

Plan: `docs/plans/2026-10-05-unified-route-evidence.md` rev 2.2, section 5 items 4 and 5.
No coordinator and no companion: nodes, operator evidence and timing records are
built by hand so each invariant is hit directly.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from decoy_engine.execution import _unified_slice_admission as admission
from decoy_engine.execution._unified_slice import UnifiedSliceInvariantError
from decoy_engine.execution.native import _chunked_evidence
from decoy_engine.execution.native._chunked_evidence import (
    ARROW_PYTHON,
    PANDAS_ORACLE,
    RUST_COMPANION,
    RUST_POOL_SELECT,
    ColumnPlan,
    chunk_route_evidence,
)
from decoy_engine.execution.physical._shadow_operators import OperatorCallEvidence
from decoy_engine.instrumentation.timing import StrategyTimingRecord


def _assemble(nodes: Any, evidence: Any, timings: Any) -> dict[str, dict[str, Any]]:
    from decoy_engine.execution._unified_slice_evidence import assemble_node_evidence

    return assemble_node_evidence(nodes, evidence, timings)


def node(node_id: str, strategy: str, column: str, operator: str | None) -> Any:
    execution = None if operator is None else SimpleNamespace(operator_id=operator)
    return SimpleNamespace(
        node_id=node_id, strategy=strategy, columns=(column,), execution=execution
    )


def ev(
    operator: str,
    *,
    compiled: bool = True,
    calls: int = 1,
    executed: bool = True,
    actual: str | None = None,
) -> OperatorCallEvidence:
    return OperatorCallEvidence(
        planned_operator=operator,
        actual_operator=operator if actual is None else actual,
        executed=executed,
        compiled_kernel_executed=compiled,
        batches_run=calls,
    )


def rec(strategy: str, column: str, elapsed_ms: float) -> StrategyTimingRecord:
    return StrategyTimingRecord(
        strategy_type=strategy, column=column, elapsed_ms=elapsed_ms, peak_memory_delta_kb=0
    )


HASH = "native_keyed_hash"
FAKER = "native_faker_select"
REDACT = "native_redact"


# ---------------------------------------------------------------------------
# Timing attribution: same strategy on two columns, records in shuffled order.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "strategy, operator, compiled",
    [("redact", REDACT, False), ("hash", HASH, True)],
)
def test_timings_are_attributed_by_strategy_and_column(
    strategy: str, operator: str, compiled: bool
) -> None:
    from decoy_engine.execution import _unified_slice_evidence as evidence_mod

    nodes = [node("n1", strategy, "a", operator), node("n2", strategy, "b", operator)]
    evidence = {
        "n1": ev(operator, compiled=compiled, calls=3),
        "n2": ev(operator, compiled=compiled, calls=7),
    }
    # Shuffled relative to node order; distinct values per column.
    timings = [rec(strategy, "b", 9.87654321), rec(strategy, "a", 1.2344)]
    assert evidence_mod._timing_by_node(nodes, timings) == {"n1": 1.2344, "n2": 9.87654321}
    out = _assemble(nodes, evidence, timings)
    # Elapsed time stays out of quality_metrics so the evidence is deterministic.
    assert all("elapsed_ms" not in entry for entry in out.values())
    assert out["n1"]["calls"] == 3
    assert out["n2"]["calls"] == 7
    json.dumps(out, allow_nan=False)


def test_full_node_dict_for_each_backend_family() -> None:
    nodes = [
        node("h", "hash", "h", HASH),
        node("f", "faker", "f", FAKER),
        node("r", "redact", "r", REDACT),
    ]
    evidence = {"h": ev(HASH), "f": ev(FAKER), "r": ev(REDACT, compiled=False)}
    timings = [rec("hash", "h", 1.0), rec("faker", "f", 2.0), rec("redact", "r", 3.0)]
    out = _assemble(nodes, evidence, timings)
    assert out == {
        "h": {
            "operator": HASH,
            "executed": True,
            "compiled_kernel_executed": True,
            "planned_backend": RUST_COMPANION,
            "executed_backend": RUST_COMPANION,
            "calls": 1,
        },
        "f": {
            "operator": FAKER,
            "executed": True,
            "compiled_kernel_executed": True,
            "planned_backend": RUST_POOL_SELECT,
            "executed_backend": RUST_POOL_SELECT,
            "calls": 1,
        },
        "r": {
            "operator": REDACT,
            "executed": True,
            "compiled_kernel_executed": False,
            "planned_backend": ARROW_PYTHON,
            "executed_backend": ARROW_PYTHON,
            "calls": 1,
        },
    }


# ---------------------------------------------------------------------------
# Invariant failures.
# ---------------------------------------------------------------------------


def _one_hash() -> tuple[list[Any], dict[str, OperatorCallEvidence], list[StrategyTimingRecord]]:
    return [node("n", "hash", "c", HASH)], {"n": ev(HASH)}, [rec("hash", "c", 1.0)]


def test_a_missing_node_raises() -> None:
    nodes, _evidence, timings = _one_hash()
    with pytest.raises(UnifiedSliceInvariantError):
        _assemble(nodes, {}, timings)


def test_a_not_executed_node_raises() -> None:
    nodes, _evidence, timings = _one_hash()
    with pytest.raises(UnifiedSliceInvariantError):
        _assemble(nodes, {"n": ev(HASH, executed=False)}, timings)


def test_an_operator_mismatch_raises() -> None:
    nodes, _evidence, timings = _one_hash()
    with pytest.raises(UnifiedSliceInvariantError):
        _assemble(nodes, {"n": ev(HASH, actual=REDACT)}, timings)


def test_a_node_without_a_binding_raises() -> None:
    _nodes, evidence, timings = _one_hash()
    with pytest.raises(UnifiedSliceInvariantError):
        _assemble([node("n", "hash", "c", None)], evidence, timings)


@pytest.mark.parametrize(
    "strategy, operator", [("hash", HASH), ("faker", FAKER)], ids=["hash", "faker"]
)
def test_a_d7_miss_raises(strategy: str, operator: str) -> None:
    with pytest.raises(UnifiedSliceInvariantError):
        _assemble(
            [node("n", strategy, "c", operator)],
            {"n": ev(operator, compiled=False)},
            [rec(strategy, "c", 1.0)],
        )


def test_a_duplicate_timing_record_raises() -> None:
    nodes, evidence, _timings = _one_hash()
    with pytest.raises(UnifiedSliceInvariantError):
        _assemble(nodes, evidence, [rec("hash", "c", 1.0), rec("hash", "c", 2.0)])


def test_a_missing_timing_record_raises() -> None:
    nodes, evidence, _timings = _one_hash()
    with pytest.raises(UnifiedSliceInvariantError):
        _assemble(nodes, evidence, [])


def test_an_extra_timing_record_for_a_non_admitted_pair_raises() -> None:
    nodes, evidence, timings = _one_hash()
    with pytest.raises(UnifiedSliceInvariantError):
        _assemble(nodes, evidence, [*timings, rec("redact", "other", 1.0)])


@pytest.mark.parametrize(
    "strategy, operator",
    [
        ("bucket_perturb", "native_bucket_perturb"),
        ("date_shift", "native_date_shift"),
        ("group_key", "native_group_key"),
    ],
)
def test_kernel_idle_value_dependent_operators_do_not_raise(strategy: str, operator: str) -> None:
    out = _assemble(
        [node("n", strategy, "c", operator)],
        {"n": ev(operator, compiled=False)},
        [rec(strategy, "c", 1.0)],
    )
    assert out["n"]["compiled_kernel_executed"] is False
    assert out["n"]["planned_backend"] == RUST_COMPANION
    assert out["n"]["executed_backend"] == ARROW_PYTHON


def test_a_compiled_value_dependent_operator_keeps_its_planned_backend() -> None:
    out = _assemble(
        [node("n", "date_shift", "c", "native_date_shift")],
        {"n": ev("native_date_shift", compiled=True)},
        [rec("date_shift", "c", 1.0)],
    )
    assert out["n"]["executed_backend"] == RUST_COMPANION


# ---------------------------------------------------------------------------
# 5. One executed-backend rule shared by both routes.
# ---------------------------------------------------------------------------


def test_unified_and_chunked_routes_share_one_executed_backend_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def patched(*args: Any, **kwargs: Any) -> str:
        seen.append("called")
        return "patched_backend"

    monkeypatch.setattr(_chunked_evidence, "executed_backend", patched)

    unified = _assemble([node("n", "hash", "c", HASH)], {"n": ev(HASH)}, [rec("hash", "c", 1.0)])
    assert unified["n"]["executed_backend"] == "patched_backend"

    chunked = chunk_route_evidence(
        table="t",
        native_admitted=True,
        reroute_reason=None,
        columns=[ColumnPlan("c", "hash", RUST_COMPANION)],
        elapsed_ms={"c": 1.0},
    )
    assert chunked["columns"][0]["executed_backend"] == "patched_backend"
    assert len(seen) == 2


def test_shared_rule_values() -> None:
    rule = _chunked_evidence.executed_backend
    assert rule(RUST_COMPANION, native_admitted=False, kernel_idle=False) == PANDAS_ORACLE
    assert rule(RUST_COMPANION, native_admitted=True, kernel_idle=True) == ARROW_PYTHON
    assert rule(RUST_COMPANION, native_admitted=True, kernel_idle=False) == RUST_COMPANION
    assert rule(RUST_POOL_SELECT, native_admitted=True, kernel_idle=False) == RUST_POOL_SELECT


def test_assembler_covers_exactly_the_admission_map() -> None:
    """Every admitted operator can be assembled; the planned backend comes from the map."""
    for operator, planned in admission.BACKEND_BY_OPERATOR_ID.items():
        compiled = planned != ARROW_PYTHON
        out = _assemble(
            [node("n", "x", "c", operator)],
            {"n": ev(operator, compiled=compiled)},
            [rec("x", "c", 1.0)],
        )
        assert out["n"]["planned_backend"] == planned

"""Task 4.4 C1/C6: `ShadowCoordinator` unit tests -- no sink/publisher/target
surface, zero-row batch synthesis, resource-budget/route-mismatch coded
failures, and the degenerate-schema assembly rules against the empirically
pinned oracle behavior (re-verified live in `test_shadow_corpus.py`).
"""

from __future__ import annotations

import inspect

import pyarrow as pa
import pytest

from decoy_engine.execution.physical._plan import (
    ExecutionBinding,
    PhysicalNode,
    PhysicalPlan,
    PhysicalTable,
)
from decoy_engine.execution.physical._shadow_context import ShadowContext
from decoy_engine.execution.physical._shadow_coordinator import (
    ShadowCoordinator,
    _assemble_column,
    _batches,
)
from decoy_engine.execution.physical._shadow_diff_codes import (
    DUPLICATE_NODE_DECLARATION,
    PLANNED_VS_ACTUAL_ROUTE_DIFF,
    ShadowDifference,
)
from decoy_engine.execution.physical._shadow_operators import OperatorCallEvidence
from decoy_engine.execution.physical._shadow_snapshot import capture_shadow_snapshot
from decoy_engine.execution.physical._types import DriverId


def _binding(operator_id: str, *, resolved_config: tuple = ()) -> ExecutionBinding:
    return ExecutionBinding(
        operator_id=operator_id,
        operator_reason=f"slice_native_admitted:{operator_id}",
        resolved_config=resolved_config,
        input_schema=pa.schema([pa.field("c", pa.string())]),
        output_schema=pa.schema([pa.field("c", pa.string())]),
        determinism_family=None,
        determinism_version=2,
        key_binding=None,
        diagnostic_obligations=(),
        required_prepasses=(),
        batch_estimate=None,
    )


def _plan_with_one_node(
    strategy: str, operator_id: str, *, resolved_config: tuple = ()
) -> PhysicalPlan:
    node = PhysicalNode(
        node_id="t:c:scalar:" + strategy,
        table="t",
        columns=("c",),
        kind="scalar",
        strategy=strategy,
        fallback_policy="native",
        provider_class=None,
        execution=_binding(operator_id, resolved_config=resolved_config),
    )
    table = PhysicalTable(
        table="t",
        driver=DriverId.FULL_FRAME,
        driver_reason="test",
        driver_reason_detail=None,
        rejected_alternatives=(),
        relationship_role="independent",
        substrate="pandas",
        nodes=(node,),
    )
    return PhysicalPlan(
        engine_version="test", plan_hash="deadbeef", synthesis=None, tables=(table,)
    )


# ---------------------------------------------------------------------------
# No sink/publisher/target surface (C1, C7).
# ---------------------------------------------------------------------------


def test_duplicate_node_id_raises_coded_difference() -> None:
    """Codex final-gate MEDIUM: two nodes with the same node_id (a config
    declaring the same column+strategy twice, which config accepts) would
    collapse into one route_evidence record + one output column. The
    coordinator must surface it with DUPLICATE_NODE_DECLARATION, not hide it."""
    from dataclasses import replace

    plan = _plan_with_one_node("passthrough", "native_passthrough")
    node = plan.tables[0].nodes[0]
    table = replace(plan.tables[0], nodes=(node, node))  # same node_id twice
    plan = replace(plan, tables=(table,))

    snapshot = capture_shadow_snapshot(
        {"t": pa.table({"c": pa.array(["a", "b"], type=pa.string())})}
    )
    ctx = ShadowContext(mask_key=b"\x03" * 32)
    with pytest.raises(ShadowDifference) as exc:
        ShadowCoordinator(ctx=ctx).run(plan, snapshot)
    assert exc.value.code == DUPLICATE_NODE_DECLARATION


def test_shadow_coordinator_has_no_sink_or_publisher_parameter() -> None:
    ctor_params = set(inspect.signature(ShadowCoordinator.__init__).parameters)
    run_params = set(inspect.signature(ShadowCoordinator.run).parameters)
    for forbidden in ("sink", "publisher", "target", "writer", "callback"):
        assert forbidden not in ctor_params
        assert forbidden not in run_params


# ---------------------------------------------------------------------------
# Zero-row batch synthesis (C6).
# ---------------------------------------------------------------------------


def test_batches_synthesizes_one_zero_row_batch_for_an_empty_table() -> None:
    empty = pa.table({"c": pa.array([], type=pa.string())})
    batches = _batches(empty, 50)
    assert len(batches) == 1
    assert batches[0].num_rows == 0


def test_batches_covers_a_ragged_final_chunk() -> None:
    table = pa.table({"c": pa.array(list(range(7)), type=pa.int64())})
    batches = _batches(table, 3)
    assert [b.num_rows for b in batches] == [3, 3, 1]


def test_coordinator_runs_the_hash_kernel_over_the_synthesized_empty_batch() -> None:
    plan = _plan_with_one_node("hash", "native_keyed_hash", resolved_config=())
    # Rebind with a real KeyBinding (the fixture above uses key_binding=None).
    from dataclasses import replace

    from decoy_engine.execution.physical._plan import KeyBinding

    node = plan.tables[0].nodes[0]
    bound = replace(
        node, execution=replace(node.execution, key_binding=KeyBinding("mask_key", "n"))
    )
    table = replace(plan.tables[0], nodes=(bound,))
    plan = replace(plan, tables=(table,))

    empty = pa.table({"c": pa.array([], type=pa.string())})
    snapshot = capture_shadow_snapshot({"t": empty})
    ctx = ShadowContext(mask_key=b"\x02" * 32)
    result = ShadowCoordinator(ctx=ctx).run(plan, snapshot)

    evidence = result.route_evidence["t:c:scalar:hash"]
    assert evidence.executed is True
    assert evidence.compiled_kernel_executed is True
    assert result.outputs["t"].num_rows == 0


# ---------------------------------------------------------------------------
# Coded failures (C4).
# ---------------------------------------------------------------------------


def test_planned_vs_actual_route_diff_is_raised_on_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan_with_one_node("passthrough", "native_passthrough")
    source = pa.table({"c": pa.array(["a", "b"], type=pa.string())})
    snapshot = capture_shadow_snapshot({"t": source})
    ctx = ShadowContext(mask_key=b"\x03" * 32)

    def _wrong_operator(array, *, binding, ctx, evidence):
        evidence.actual_operator = "not-the-planned-operator"
        evidence.executed = True
        return array

    monkeypatch.setattr(
        "decoy_engine.execution.physical._shadow_coordinator.run_operator", _wrong_operator
    )
    with pytest.raises(ShadowDifference) as excinfo:
        ShadowCoordinator(ctx=ctx).run(plan, snapshot)
    assert excinfo.value.code == PLANNED_VS_ACTUAL_ROUTE_DIFF


# ---------------------------------------------------------------------------
# Degenerate-schema output assembly (C3), pinned against the live oracle
# probe recorded in the build (test_shadow_corpus.py re-verifies these
# live).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("strategy", "input_type", "expected_type"),
    [
        ("redact", pa.string(), pa.float64()),
        ("truncate", pa.string(), pa.float64()),
        ("hash", pa.string(), pa.float64()),
        ("passthrough", pa.int64(), pa.int64()),
        ("passthrough", pa.bool_(), pa.bool_()),
        ("passthrough", pa.string(), pa.null()),
    ],
)
def test_assemble_column_empty(
    strategy: str, input_type: pa.DataType, expected_type: pa.DataType
) -> None:
    native_type = pa.string() if strategy != "passthrough" else input_type
    parts = [pa.array([], type=native_type)]
    out = _assemble_column(strategy, parts)
    assert out.type.equals(expected_type)
    assert len(out) == 0


@pytest.mark.parametrize(
    ("strategy", "input_type", "expected_type"),
    [
        ("redact", pa.string(), pa.null()),
        ("truncate", pa.string(), pa.null()),
        ("hash", pa.string(), pa.null()),
        ("passthrough", pa.int64(), pa.float64()),
        ("passthrough", pa.string(), pa.null()),
    ],
)
def test_assemble_column_all_null(
    strategy: str, input_type: pa.DataType, expected_type: pa.DataType
) -> None:
    native_type = pa.string() if strategy != "passthrough" else input_type
    parts = [pa.array([None, None], type=native_type)]
    out = _assemble_column(strategy, parts)
    assert out.type.equals(expected_type)
    assert out.to_pylist() == [None, None]


def test_assemble_column_normal_case_keeps_native_type() -> None:
    parts = [pa.array(["a", None, "b"], type=pa.string())]
    out = _assemble_column("redact", parts)
    assert out.type.equals(pa.string())
    assert out.to_pylist() == ["a", None, "b"]


def test_assemble_column_partial_null_int_passthrough_upcasts_to_float() -> None:
    parts = [pa.array([1, None, 3], type=pa.int64())]
    out = _assemble_column("passthrough", parts)
    assert out.type.equals(pa.float64())
    assert out.to_pylist() == [1.0, None, 3.0]


# ---------------------------------------------------------------------------
# run_operator's own default resolution + batch-count bookkeeping.
# ---------------------------------------------------------------------------


def test_run_operator_truncate_defaults_keep_to_head_when_config_omits_it() -> None:
    """`resolved_config` normally always carries `keep` (`_shadow_bindings`
    resolves it at compile time); `run_operator`'s own `cfg.get("keep",
    "head")` fallback is a second, independently testable default."""
    from decoy_engine.execution.physical._shadow_operators import run_operator

    binding = _binding("native_truncate", resolved_config=(("length", 3),))
    ctx = ShadowContext(mask_key=b"\x05" * 32)
    evidence = OperatorCallEvidence(planned_operator=binding.operator_id)
    array = pa.array(["abcdef"], type=pa.string())

    out = run_operator(array, binding=binding, ctx=ctx, evidence=evidence)
    assert out.to_pylist() == ["abc"]  # head-kept, not tail


def test_run_operator_truncate_falls_back_to_length_zero_which_fails_closed() -> None:
    """When `resolved_config` carries no valid int `length`, the fallback
    must be `0` (which `native_truncate` itself rejects), not `1` (which
    would silently succeed with a 1-char truncation instead of failing)."""
    from decoy_engine.execution._errors import StrategyError
    from decoy_engine.execution.physical._shadow_operators import run_operator

    binding = _binding("native_truncate", resolved_config=())
    ctx = ShadowContext(mask_key=b"\x06" * 32)
    evidence = OperatorCallEvidence(planned_operator=binding.operator_id)
    array = pa.array(["abcdef"], type=pa.string())

    with pytest.raises(StrategyError, match="truncate_length_invalid"):
        run_operator(array, binding=binding, ctx=ctx, evidence=evidence)


def test_route_evidence_batches_run_counts_every_batch() -> None:
    plan = _plan_with_one_node("passthrough", "native_passthrough")
    source = pa.table({"c": pa.array(list(range(7)), type=pa.int64())})
    snapshot = capture_shadow_snapshot({"t": source})
    ctx = ShadowContext(mask_key=b"\x07" * 32, batch_size_rows=3)

    result = ShadowCoordinator(ctx=ctx).run(plan, snapshot)
    assert result.route_evidence["t:c:scalar:passthrough"].batches_run == 3  # 3, 3, 1

"""C8-ii acceptance: `when:` on the unified full-frame route.

Plan: `docs/plans/2026-10-07-c8-ii-unified-when.md` rev 2, section 4. Rules run through the file:

- Every differential compares the lane-on run with an explicit lane-off run: output tables
  (schema and `b"pandas"` metadata included), warnings, row errors and every quality metric
  except the activation leaf, which is checked separately.
- Admitted cases poison the pandas oracle, so a silent reroute fails. Decline cases run the
  lane unpoisoned and assert the lane did not activate.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.execution import _unified_slice_admission as admission
from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._unified_slice import QUALITY_METRICS_KEY, UnifiedSliceInvariantError
from decoy_engine.execution.native._companion_status import native_companion_status
from decoy_engine.execution.physical._compiler import compile_physical_plan
from decoy_engine.execution.physical._shadow_context import ShadowContext
from decoy_engine.execution.physical._shadow_coordinator import ShadowCoordinator
from decoy_engine.execution.physical._shadow_diff_codes import ShadowDifference
from decoy_engine.execution.physical._shadow_operators import OperatorCallEvidence
from decoy_engine.execution.physical._shadow_snapshot import capture_shadow_snapshot
from decoy_engine.execution.physical._snapshot import capture_physical_plan_inputs
from decoy_engine.instrumentation.timing import StrategyTimingRecord, TimingCollector, use_collector
from decoy_engine.providers_v2 import ProviderRegistry, get_default_registry
from decoy_engine.providers_v2._errors import ProviderError
from tests.physical._shadow_helpers import build_config, write_read_only_fixture
from tests.physical.test_unified_slice_faker import (
    Case,
    CountingAdapter,
    _key_provider,
    lane_run,
)
from tests.physical.test_unified_slice_parity import _assert_full_parity
from tests.physical.test_unified_slice_positional import lane_batch_rows

NEEDS_COMPANION = pytest.mark.skipif(
    not native_companion_status().ok,
    reason="compiled decoy-engine-native companion unavailable",
)

CATEGORIES = ["alpha", "beta", "gamma"]


def target(strategy: str, name: str = "c") -> dict[str, Any]:
    if strategy == "hash":
        return {"name": name, "strategy": "hash", "namespace": "ns_h"}
    if strategy == "redact":
        return {"name": name, "strategy": "redact"}
    if strategy == "truncate":
        return {"name": name, "strategy": "truncate", "provider_config": {"length": 3}}
    return {
        "name": name,
        "strategy": "categorical",
        "namespace": "ns_cat",
        "deterministic": True,
        "provider_config": {"categories": CATEGORIES},
    }


STRATEGIES = [
    pytest.param("hash", id="hash", marks=NEEDS_COMPANION),
    pytest.param("redact", id="redact"),
    pytest.param("truncate", id="truncate"),
    pytest.param("categorical", id="categorical", marks=NEEDS_COMPANION),
]
PASS_S = {"name": "s", "strategy": "passthrough"}


def with_when(by_column: dict[str, str]) -> Callable[[dict[str, Any]], None]:
    """`when` is not a declared `ColumnConfig` field, so it is set on the validated raw dict."""

    def apply(config: dict[str, Any]) -> None:
        for col in config["tables"][0]["columns"]:
            if col["name"] in by_column:
                col["when"] = by_column[col["name"]]

    return apply


def source(n: int = 24, *, null_c: bool = True, sidecar: bool = False) -> pa.Table:
    c = [None if null_c and i % 7 == 3 else f"src_{i % 5}" for i in range(n)]
    s = ["x" if i % 3 == 0 else "y" for i in range(n)]
    if sidecar:
        frame = pd.DataFrame({"c": pd.array(c, dtype="string"), "s": pd.array(s, dtype="string")})
        return pa.Table.from_pandas(frame, preserve_index=False)
    return pa.table({"c": pa.array(c, type=pa.string()), "s": pa.array(s, type=pa.string())})


def admitted(case: Case, **kwargs: Any) -> tuple[ExecutionResult, dict[str, Any]]:
    """Lane-on (oracle poisoned) equals lane-off; returns the lane result and its leaf."""
    off = case.run(lane=False, **kwargs)
    on = lane_run(case, **kwargs)
    leaf = _assert_full_parity(off, on)
    return on, leaf


def declined(case: Case, **kwargs: Any) -> ExecutionResult:
    """The lane did not activate and the result equals lane-off."""
    off = case.run(lane=False, **kwargs)
    on = case.run(lane=True, **kwargs)
    assert QUALITY_METRICS_KEY not in on.quality_metrics
    assert on.quality_metrics == off.quality_metrics
    assert on.outputs["t"].equals(off.outputs["t"], check_metadata=True)
    assert tuple(on.warnings) == tuple(off.warnings)
    assert tuple(on.row_errors) == tuple(off.row_errors)
    return on


# ---------------------------------------------------------------------------
# 1. Differential matrix.
# ---------------------------------------------------------------------------

PREDICATES = {
    "zero": "s == 'nope'",
    "partial": "s == 'x'",
    "all": "s in ['x', 'y']",
}


@pytest.mark.parametrize("strategy", STRATEGIES)
@pytest.mark.parametrize("selectivity", sorted(PREDICATES))
def test_1_strategy_by_selectivity(tmp_path: Path, strategy: str, selectivity: str) -> None:
    case = Case(
        tmp_path,
        source(),
        [target(strategy), PASS_S],
        mutate=with_when({"c": PREDICATES[selectivity]}),
    )
    on, leaf = admitted(case)
    node = next(e for e in leaf["nodes"].values() if e["operator"] != "native_passthrough")
    assert node["executed"] is True
    if selectivity == "zero" and strategy in ("hash", "categorical"):
        # Nothing selected: no kernel ran, so the node reports idle evidence.
        assert node["compiled_kernel_executed"] is False
        assert node["executed_backend"] == "arrow_python"
    if selectivity != "zero" and strategy in ("hash", "categorical"):
        assert node["compiled_kernel_executed"] is True


@pytest.mark.parametrize("strategy", STRATEGIES)
@pytest.mark.parametrize(
    "expr",
    ["c != 'src_1'", "c == 'src_1'", "c in ['src_0', 'src_2']", "c not in ['src_0']"],
    ids=["ne_selects_nulls", "eq", "in_list", "not_in_list"],
)
def test_1_self_reference_and_nulls(tmp_path: Path, strategy: str, expr: str) -> None:
    case = Case(tmp_path, source(), [target(strategy), PASS_S], mutate=with_when({"c": expr}))
    admitted(case)


@pytest.mark.parametrize("strategy", STRATEGIES)
@pytest.mark.parametrize("sidecar", [False, True], ids=["bare_arrow", "stringdtype_sidecar"])
@pytest.mark.parametrize("batch", [None, 5], ids=["one_batch", "ragged_batches"])
def test_1_sidecar_and_batches(
    tmp_path: Path, strategy: str, sidecar: bool, batch: int | None
) -> None:
    case = Case(
        tmp_path,
        source(23, sidecar=sidecar),
        [target(strategy), PASS_S],
        mutate=with_when({"c": "s == 'x'"}),
    )
    if batch is None:
        admitted(case)
        return
    off = case.run(lane=False)
    with lane_batch_rows(batch):
        on = lane_run(case)
    _assert_full_parity(off, on)


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_1_empty_table(tmp_path: Path, strategy: str) -> None:
    empty = pa.table({"c": pa.array([], type=pa.string()), "s": pa.array([], type=pa.string())})
    case = Case(tmp_path, empty, [target(strategy), PASS_S], mutate=with_when({"c": "s == 'x'"}))
    admitted(case)


def _int64_sidecar_source(n: int = 20) -> pa.Table:
    frame = pd.DataFrame(
        {
            "c": [f"src_{i % 4}" for i in range(n)],
            "n": pd.array([None if i % 4 == 1 else i % 5 for i in range(n)], dtype="Int64"),
        }
    )
    return pa.Table.from_pandas(frame, preserve_index=False)


PASS_N = {"name": "n", "strategy": "passthrough"}


@pytest.mark.parametrize("strategy", ["redact", pytest.param("hash", marks=NEEDS_COMPANION)])
@pytest.mark.parametrize("expr", ["n > 1", "n <= 2", "n == 3"])
def test_1_nullable_int_sibling_through_the_pandas_sidecar(
    tmp_path: Path, strategy: str, expr: str
) -> None:
    case = Case(
        tmp_path,
        _int64_sidecar_source(),
        [target(strategy), PASS_N],
        mutate=with_when({"c": expr}),
    )
    admitted(case)


@NEEDS_COMPANION
def test_1_nullable_int_reached_through_a_group_key_protected_sibling(tmp_path: Path) -> None:
    n = 20
    values = [None if i % 4 == 1 else i % 5 for i in range(n)]
    plain = pa.table(
        {
            "c": pa.array([f"src_{i % 4}" for i in range(n)], type=pa.string()),
            "n": pa.array(values, type=pa.int64()),
            "g": pa.array([f"k{i}" for i in range(n)], type=pa.string()),
        }
    )
    columns = [
        target("redact"),
        PASS_N,
        {
            "name": "g",
            "strategy": "group_key",
            "namespace": "ns_g",
            "provider_config": {"group_by": "n", "length": 8},
        },
    ]
    case = Case(tmp_path, plain, columns, mutate=with_when({"c": "n > 1"}))
    admitted(case)


def test_1_plain_arrow_int_with_nulls_declines(tmp_path: Path) -> None:
    """Without a sidecar or a group_key, pandas reads the nullable int as float: the
    round-trip check declines the table, and the output is lane-off's."""
    n = 20
    plain = pa.table(
        {
            "c": pa.array([f"src_{i % 4}" for i in range(n)], type=pa.string()),
            "n": pa.array([None if i % 4 == 1 else i % 5 for i in range(n)], type=pa.int64()),
        }
    )
    case = Case(tmp_path, plain, [target("redact"), PASS_N], mutate=with_when({"c": "n > 1"}))
    declined(case)


def test_1_float_passthrough_sibling_declines(tmp_path: Path) -> None:
    n = 12
    src = pa.table(
        {
            "c": pa.array([f"src_{i % 4}" for i in range(n)], type=pa.string()),
            "f": pa.array([float(i) for i in range(n)], type=pa.float64()),
        }
    )
    columns = [target("redact"), {"name": "f", "strategy": "passthrough"}]
    case = Case(tmp_path, src, columns, mutate=with_when({"c": "f > 3.5"}))
    declined(case)


# ---------------------------------------------------------------------------
# 2. Several `when` columns in one table.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
def test_2_several_when_columns_one_table(tmp_path: Path) -> None:
    n = 30
    src = pa.table(
        {
            "a": pa.array([None if i % 6 == 0 else f"a{i % 4}" for i in range(n)], pa.string()),
            "b": pa.array([f"b{i % 5}" for i in range(n)], pa.string()),
            "d": pa.array([f"d{i % 3}" for i in range(n)], pa.string()),
            "s": pa.array(["x" if i % 2 else "y" for i in range(n)], pa.string()),
            "z": pa.array([f"z{i}" for i in range(n)], pa.string()),
        }
    )
    columns = [
        target("hash", "a"),
        target("redact", "b"),
        target("truncate", "d"),
        PASS_S,
        {"name": "z", "strategy": "passthrough"},
    ]
    # `a` and `b` read the same sibling; `d` follows non-when work it does not reference.
    when = {"a": "s == 'x'", "b": "s != 'x'", "d": "z != 'z3'"}
    case = Case(tmp_path, src, columns, mutate=with_when(when))
    _, leaf = admitted(case)
    assert len(leaf["nodes"]) == 5
    for batch in (7,):
        off = case.run(lane=False)
        with lane_batch_rows(batch):
            on = lane_run(case)
        _assert_full_parity(off, on)


# ---------------------------------------------------------------------------
# 3. Rule 4 counterexample.
# ---------------------------------------------------------------------------


def test_3_a_reference_to_a_column_an_earlier_node_masks_declines(tmp_path: Path) -> None:
    n = 16
    src = pa.table(
        {
            "a": pa.array([f"src_{i % 3}" for i in range(n)], pa.string()),
            "c": pa.array([f"src_{i % 5}" for i in range(n)], pa.string()),
        }
    )
    # Work order is by column name, so `a` runs first and the predicate on `c` reads it masked.
    columns = [target("redact", "a"), target("redact", "c")]
    case = Case(tmp_path, src, columns, mutate=with_when({"c": "a == 'src_0'"}))
    declined(case)


# ---------------------------------------------------------------------------
# 4. Declines.
# ---------------------------------------------------------------------------


def test_4_a_reference_not_in_the_source_declines_and_raises_the_oracles_error(
    tmp_path: Path,
) -> None:
    case = Case(
        tmp_path, source(), [target("redact"), PASS_S], mutate=with_when({"c": "nope == 'x'"})
    )
    with pytest.raises(StrategyError) as off_info:
        case.run(lane=False)
    with pytest.raises(StrategyError) as on_info:
        case.run(lane=True)
    assert on_info.value.code == off_info.value.code == "when_expression_error"
    assert str(on_info.value) == str(off_info.value)


@pytest.mark.parametrize("expr", ["s.notnull()", "s == c"])
def test_4_a_predicate_outside_the_closed_grammar_is_rejected_at_compile(
    tmp_path: Path, expr: str
) -> None:
    """Compile rejects it before either lane runs. The admission-level decline (the lane's
    `when_columns_admitted` is False for such an entry) is asserted directly in
    `tests/unit/execution/test_c8_iii_c_boundaries.py`."""
    from decoy_engine.plan._errors import PlanCompileError

    case = Case(tmp_path, source(), [target("redact"), PASS_S], mutate=with_when({"c": expr}))
    for lane in (False, True):
        with pytest.raises(PlanCompileError) as info:
            case.run(lane=lane)
        assert info.value.code == "when_outside_closed_grammar"


ALIAS = "BACKTICK_QUOTED_STRING_a_b"


def _alias_source(n: int = 12) -> pa.Table:
    return pa.table(
        {
            "c": pa.array([f"src_{i % 4}" for i in range(n)], pa.string()),
            "a b": pa.array(["x" if i % 2 else "y" for i in range(n)], pa.string()),
            ALIAS: pa.array(["p" if i % 3 else "q" for i in range(n)], pa.string()),
        }
    )


def test_4_names_that_collide_under_the_resolver_normalization_decline(tmp_path: Path) -> None:
    columns = [
        target("redact"),
        {"name": "a b", "strategy": "passthrough"},
        {"name": ALIAS, "strategy": "passthrough"},
    ]
    case = Case(tmp_path, _alias_source(), columns, mutate=with_when({"c": "c == 'src_1'"}))
    declined(case)


def test_4_a_collision_where_an_earlier_node_writes_the_aliasing_column_declines(
    tmp_path: Path,
) -> None:
    columns = [
        target("redact"),
        {"name": "a b", "strategy": "passthrough"},
        target("redact", ALIAS),
    ]
    case = Case(tmp_path, _alias_source(), columns, mutate=with_when({"c": "c == 'src_1'"}))
    declined(case)


def test_4_a_table_without_when_is_not_affected_by_the_collision_decline(tmp_path: Path) -> None:
    columns = [
        target("redact"),
        {"name": "a b", "strategy": "passthrough"},
        {"name": ALIAS, "strategy": "passthrough"},
    ]
    case = Case(tmp_path, _alias_source(), columns)
    admitted(case)


# ---------------------------------------------------------------------------
# 5. Fail closed.
# ---------------------------------------------------------------------------


def _compiled(tmp_path: Path, strategy: str) -> tuple[Any, Any, Any, Any]:
    src = source()
    case = Case(tmp_path, src, [target(strategy), PASS_S], mutate=with_when({"c": "s == 'x'"}))
    inputs = capture_physical_plan_inputs(case.config, {"t": src}, engine_version="c8-ii-test")
    return case, inputs, compile_physical_plan(inputs), src


def test_5_the_binding_carries_the_predicate(tmp_path: Path) -> None:
    _, _, plan, _ = _compiled(tmp_path, "redact")
    by_column = {n.columns[0]: n.execution for n in plan.tables[0].nodes}
    assert by_column["c"] is not None and by_column["c"].when_expression == "s == 'x'"
    assert by_column["s"] is not None and by_column["s"].when_expression is None


def _coordinator_run(inputs: Any, plan: Any, src: pa.Table, **kwargs: Any) -> Any:
    ctx = ShadowContext.from_key_provider(plan=inputs.plan, key_provider=_key_provider())
    return ShadowCoordinator(ctx=ctx, registry=inputs.registry).run(
        plan, capture_shadow_snapshot({"t": src}), **kwargs
    )


def test_5_a_when_node_without_a_mask_raises_shadow_difference(tmp_path: Path) -> None:
    _, inputs, plan, src = _compiled(tmp_path, "redact")
    with pytest.raises(ShadowDifference):
        _coordinator_run(inputs, plan, src)


def test_5_a_when_node_with_a_mask_for_another_node_still_raises(tmp_path: Path) -> None:
    _, inputs, plan, src = _compiled(tmp_path, "redact")
    other = pa.array([True] * src.num_rows, type=pa.bool_())
    with pytest.raises(ShadowDifference):
        _coordinator_run(inputs, plan, src, when_masks={"not-this-node": other})


def test_5_a_mask_makes_the_coordinator_run(tmp_path: Path) -> None:
    _, inputs, plan, src = _compiled(tmp_path, "redact")
    node = next(n for n in plan.tables[0].nodes if n.columns == ("c",))
    mask = pa.array([i % 2 == 0 for i in range(src.num_rows)], type=pa.bool_())
    result = _coordinator_run(inputs, plan, src, when_masks={node.node_id: mask})
    out = result.outputs["t"].column("c").to_pylist()
    base = src.column("c").to_pylist()
    assert [o == b for o, b in zip(out, base, strict=True)][1::2] == [True] * (len(base) // 2)
    assert result.route_evidence[node.node_id].rows_selected == int(
        np.sum(mask.to_numpy(zero_copy_only=False))
    )


# ---------------------------------------------------------------------------
# 6. Error parity and order.
# ---------------------------------------------------------------------------


def _failing_registry() -> ProviderRegistry:
    default = get_default_registry()
    err = ProviderError(code="first_name_fail", message="first_name_fail message")
    return default.override(
        "person_first_name",
        CountingAdapter(lambda i: "x", fail=err),
        default.get_capabilities("person_first_name"),
    )


def _faker(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "strategy": "faker",
        "provider": "person_first_name",
        "deterministic": True,
        "namespace": f"ns_{name}",
        "pool_size": 30,
    }


def _spy_mask_step(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record each lane-side mask computation, so a test proves the lane reached that step."""
    import decoy_engine.execution._unified_slice as unified

    calls: list[int] = []
    real = unified.compute_when_masks

    def spy(frame: Any, nodes: Any) -> Any:
        calls.append(1)
        return real(frame, nodes)

    monkeypatch.setattr(unified, "compute_when_masks", spy)
    return calls


def test_6_a_predicate_that_raises_declines_before_any_node_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A string compared to a number: the oracle raises when it evaluates the mask.
    case = Case(tmp_path, source(), [target("redact"), PASS_S], mutate=with_when({"c": "s < 1"}))
    with pytest.raises(StrategyError) as off_info:
        case.run(lane=False)
    mask_calls = _spy_mask_step(monkeypatch)
    with pytest.raises(StrategyError) as on_info:
        case.run(lane=True)
    assert mask_calls, "the lane declined before its mask step, so the decline path was not tested"
    assert on_info.value.code == off_info.value.code == "when_expression_error"
    assert str(on_info.value) == str(off_info.value)


@NEEDS_COMPANION
@pytest.mark.parametrize("faker_first", [True, False], ids=["faker_first", "predicate_first"])
@pytest.mark.parametrize("config_reversed", [False, True], ids=["config_order", "config_reversed"])
def test_6_competing_failures_raise_the_oracles_first_error(
    tmp_path: Path, faker_first: bool, config_reversed: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    n = 10
    names = ("a", "b") if faker_first else ("b", "a")
    faker_name, pred_name = names
    src = pa.table(
        {
            "a": pa.array([f"v{i}" for i in range(n)], pa.string()),
            "b": pa.array([f"w{i}" for i in range(n)], pa.string()),
            "s": pa.array(["x"] * n, pa.string()),
        }
    )
    columns = [_faker(faker_name), target("redact", pred_name), PASS_S]
    if config_reversed:
        columns.reverse()
    case = Case(tmp_path, src, columns, mutate=with_when({pred_name: "s < 1"}))
    with pytest.raises(Exception) as off_info:
        case.run(lane=False, registry=_failing_registry())
    mask_calls = _spy_mask_step(monkeypatch)
    with pytest.raises(Exception) as on_info:
        case.run(lane=True, registry=_failing_registry())
    assert mask_calls, "the lane declined before its mask step, so the decline path was not tested"
    assert type(on_info.value) is type(off_info.value)
    assert getattr(on_info.value, "code", None) == getattr(off_info.value, "code", None)
    assert str(on_info.value) == str(off_info.value)
    expected = "first_name_fail" if faker_first else "when_expression_error"
    assert getattr(off_info.value, "code", None) == expected


# ---------------------------------------------------------------------------
# 7. Evidence.
# ---------------------------------------------------------------------------


def _one_node_evidence(operator: str, evidence: OperatorCallEvidence) -> dict[str, Any]:
    from decoy_engine.execution._unified_slice_evidence import assemble_node_evidence

    node = SimpleNamespace(
        node_id="n",
        strategy="hash",
        columns=("c",),
        execution=SimpleNamespace(operator_id=operator, params=None),
    )
    record = StrategyTimingRecord(
        strategy_type="hash", column="c", elapsed_ms=1.0, peak_memory_delta_kb=0
    )
    return assemble_node_evidence([node], {"n": evidence}, [record])["n"]


def _hash_evidence(*, compiled: bool, rows_seen: int | None, rows_selected: int | None):
    return OperatorCallEvidence(
        planned_operator="native_keyed_hash",
        actual_operator="native_keyed_hash",
        executed=True,
        compiled_kernel_executed=compiled,
        batches_run=1,
        rows_seen=rows_seen,
        rows_selected=rows_selected,
    )


def test_7_a_zero_selected_hash_node_completes_with_idle_evidence() -> None:
    out = _one_node_evidence(
        "native_keyed_hash", _hash_evidence(compiled=False, rows_seen=10, rows_selected=0)
    )
    assert out["compiled_kernel_executed"] is False
    assert out["executed_backend"] == "arrow_python"


def test_7_a_selected_hash_node_without_kernel_evidence_raises() -> None:
    with pytest.raises(UnifiedSliceInvariantError, match="positive"):
        _one_node_evidence(
            "native_keyed_hash", _hash_evidence(compiled=False, rows_seen=10, rows_selected=3)
        )


def test_7_a_hash_node_that_never_counted_selection_keeps_the_strict_check() -> None:
    with pytest.raises(UnifiedSliceInvariantError, match="positive"):
        _one_node_evidence(
            "native_keyed_hash", _hash_evidence(compiled=False, rows_seen=10, rows_selected=None)
        )


def test_7_the_positional_faker_exemption_is_unchanged() -> None:
    from decoy_engine.execution._unified_slice_evidence import assemble_node_evidence
    from decoy_engine.execution.native._operator_params import FakerParams

    params = FakerParams(
        namespace=None,
        positional=True,
        selection_namespace="t/c",
    )
    node = SimpleNamespace(
        node_id="n",
        strategy="faker",
        columns=("c",),
        execution=SimpleNamespace(operator_id="native_faker_select", params=params),
    )
    record = StrategyTimingRecord(
        strategy_type="faker", column="c", elapsed_ms=1.0, peak_memory_delta_kb=0
    )
    zero_rows = OperatorCallEvidence(
        planned_operator="native_faker_select",
        actual_operator="native_faker_select",
        executed=True,
        compiled_kernel_executed=False,
        batches_run=1,
        rows_seen=0,
    )
    assert assemble_node_evidence([node], {"n": zero_rows}, [record])["n"]["executed"] is True
    with_rows = OperatorCallEvidence(**{**zero_rows.__dict__, "rows_seen": 4})
    with pytest.raises(UnifiedSliceInvariantError, match="positive"):
        assemble_node_evidence([node], {"n": with_rows}, [record])


@NEEDS_COMPANION
def test_7_rows_selected_sums_across_batches(tmp_path: Path) -> None:
    case = Case(
        tmp_path,
        source(23, null_c=False),
        [target("hash"), PASS_S],
        mutate=with_when({"c": "s == 'x'"}),
    )
    src = case.source
    expected = sum(1 for v in src.column("s").to_pylist() if v == "x")
    inputs = capture_physical_plan_inputs(case.config, {"t": src}, engine_version="c8-ii-test")
    plan = compile_physical_plan(inputs)
    node = next(n for n in plan.tables[0].nodes if n.columns == ("c",))
    frame = src.to_pandas()
    mask = (frame["s"] == "x").to_numpy()
    ctx = ShadowContext.from_key_provider(
        plan=inputs.plan, key_provider=_key_provider(), batch_size_rows=5
    )
    collector = TimingCollector()
    with use_collector(collector):
        result = ShadowCoordinator(ctx=ctx, registry=inputs.registry).run(
            plan,
            capture_shadow_snapshot({"t": src}),
            when_masks={node.node_id: pa.array(mask, type=pa.bool_())},
        )
    evidence = result.route_evidence[node.node_id]
    assert evidence.batches_run == 5
    assert evidence.rows_seen == 23
    assert evidence.rows_selected == expected


# ---------------------------------------------------------------------------
# 8. Default-on switch.
# ---------------------------------------------------------------------------


def test_8_an_eligible_when_job_activates_by_default(tmp_path: Path) -> None:
    from decoy_engine.execution import run_pipeline

    case = Case(tmp_path, source(), [target("redact"), PASS_S], mutate=with_when({"c": "s == 'x'"}))
    default = run_pipeline(
        case.config,
        {"t": case.source},
        engine_version="c8-ii-test",
        key_provider=_key_provider(),
    )
    off = case.run(lane=False)
    assert QUALITY_METRICS_KEY in default.quality_metrics
    assert default.outputs["t"].equals(off.outputs["t"], check_metadata=True)


def test_8_a_job_without_when_is_unchanged(tmp_path: Path) -> None:
    case = Case(tmp_path, source(), [target("redact"), PASS_S])
    _, leaf = admitted(case)
    assert len(leaf["nodes"]) == 2


# ---------------------------------------------------------------------------
# 9. Reconstruction dtype pin.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_9_the_stringdtype_sidecar_survives_partial_selectivity(
    tmp_path: Path, strategy: str
) -> None:
    src = source(24, sidecar=True)
    meta = json.loads(src.schema.metadata[b"pandas"])
    assert {c["pandas_type"] for c in meta["columns"]} == {"unicode"}
    assert any("string" in str(c.get("numpy_type")) for c in meta["columns"])
    case = Case(tmp_path, src, [target(strategy), PASS_S], mutate=with_when({"c": "s == 'x'"}))
    off = case.run(lane=False)
    on = lane_run(case)
    assert on.outputs["t"].schema.metadata[b"pandas"] == off.outputs["t"].schema.metadata[b"pandas"]
    assert on.outputs["t"].equals(off.outputs["t"], check_metadata=True)


# ---------------------------------------------------------------------------
# Admission units.
# ---------------------------------------------------------------------------


def _cheap(tmp_path: Path, src: pa.Table, columns: list[dict[str, Any]], when: dict[str, str]):
    from decoy_engine.profile import profile_source

    path = write_read_only_fixture(tmp_path, src, "adm")
    config = build_config(tmp_path, "t", path, columns)
    with_when(when)(config)
    profile = profile_source(config, seed=20260914)
    return admission.cheap_admission(
        route="full_frame",
        route_chunked=False,
        resolved_substrate="pandas",
        sink=None,
        source_loader=None,
        fidelity_report=False,
        vault_writer=None,
        config=config,
        profile=profile,
        table_kinds={"t": "mask"},
        caller_sources={"t": src},
        registry=get_default_registry(),
    )


def test_admission_admits_an_eligible_when_column(tmp_path: Path) -> None:
    assert _cheap(tmp_path, source(), [target("redact"), PASS_S], {"c": "s == 'x'"}) is not None


def test_admission_declines_a_non_string_target(tmp_path: Path) -> None:
    src = pa.table(
        {"c": pa.array(list(range(6)), pa.int64()), "s": pa.array(list("xyxyxy"), pa.string())}
    )
    assert _cheap(tmp_path, src, [target("redact"), PASS_S], {"c": "s == 'x'"}) is None


def test_admission_declines_a_reference_outside_the_source(tmp_path: Path) -> None:
    assert _cheap(tmp_path, source(), [target("redact"), PASS_S], {"c": "q == 'x'"}) is None


def test_a_declined_predicate_failure_logs_no_predicate_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    expr = "s < 1 and s != 'sentinel_literal'"
    case = Case(tmp_path, source(), [target("redact"), PASS_S], mutate=with_when({"c": expr}))
    with caplog.at_level("DEBUG"), pytest.raises(StrategyError):
        case.run(lane=True)
    assert "sentinel_literal" not in caplog.text
    assert "s < 1" not in caplog.text


def _when_node(expression: str | None) -> Any:
    binding = SimpleNamespace(when_expression=expression)
    return SimpleNamespace(node_id="n", strategy="redact", columns=("c",), execution=binding)


def test_a_mask_of_the_wrong_length_raises_an_invariant_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decoy_engine.execution import _unified_slice_when

    monkeypatch.setattr(
        _unified_slice_when, "_eval_predicate", lambda *a, **k: pd.Series([True, False])
    )
    frame = pd.DataFrame({"c": ["a", "b", "c"]})
    with pytest.raises(UnifiedSliceInvariantError, match="rows"):
        _unified_slice_when.compute_when_masks(frame, [_when_node("c == 'a'")])


def test_a_node_without_a_predicate_gets_no_mask() -> None:
    from decoy_engine.execution._unified_slice_when import compute_when_masks

    masks = compute_when_masks(pd.DataFrame({"c": ["a"]}), [_when_node(None)])
    assert masks.selected == {} and masks.arrow == {}


def test_reconstruction_of_a_when_node_without_its_mask_raises() -> None:
    from decoy_engine.execution._unified_slice_evidence import reconstruct_source_shaped_output

    frame = pd.DataFrame({"c": ["a", "b"]})
    masked = pa.table({"c": pa.array(["X", "Y"], pa.string())})
    with pytest.raises(UnifiedSliceInvariantError, match="mask"):
        reconstruct_source_shaped_output(
            table="t",
            frame=frame,
            masked_table=masked,
            nodes=[_when_node("c == 'a'")],
            when_selected={},
        )


def test_reconstruction_writes_back_only_the_selected_rows() -> None:
    from decoy_engine.execution._unified_slice_evidence import reconstruct_source_shaped_output

    frame = pd.DataFrame({"c": pd.array(["a", None, "c"], dtype="string")})
    masked = pa.table({"c": pa.array(["X", "Y", "Z"], pa.string())})
    out = reconstruct_source_shaped_output(
        table="t",
        frame=frame,
        masked_table=masked,
        nodes=[_when_node("c == 'a'")],
        when_selected={"n": np.array([True, False, True])},
    )["t"]
    assert out.column("c").to_pylist() == ["X", None, "Z"]
    assert json.loads(out.schema.metadata[b"pandas"])["columns"][0]["numpy_type"] == "string"

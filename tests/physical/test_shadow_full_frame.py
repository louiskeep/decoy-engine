"""Task 4.6 slice 6: the coordinator's FULL_FRAME dispatch (`_shadow_full_
frame.py`) wraps the existing `FullFrameAdapter` for the deterministic
GLOBAL strategies (shuffle, top_code, grouped_series, derived_aggregate).
Six groups mirroring the plan's acceptance tests (A1-A9):

(a) A1/A7: direct-adapter fidelity -- the coordinator's `ShadowRunResult`
    against a DIRECT `FullFrameAdapter(...).run()` call, with the compiled
    plan's driver forced to and asserted as exactly `{FULL_FRAME}`.
(b) A2/A2b: oracle parity, including an extra resident source table beyond
    the one masked (output-key preservation + projection-warning parity).
(c) A3: `quality_metrics` forwarding via a sentinel test-double adapter.
(d) A4: top_code `top_code_generalized` warning + malformed-value RowError
    parity against the oracle.
(e) A5: deterministic-shuffle cross-run byte identity.
(f) A6: every decline path, each proven by a zero-invocation adapter spy.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import run_pipeline
from decoy_engine.execution._adapter import ExecutionAdapter, ExecutionResult
from decoy_engine.execution._output_projection import resolve_unconfigured_column_policy
from decoy_engine.execution._substrate import select_execution_adapter
from decoy_engine.execution.physical._compiler import compile_physical_plan
from decoy_engine.execution.physical._plan import PhysicalPlan
from decoy_engine.execution.physical._shadow_context import ShadowContext
from decoy_engine.execution.physical._shadow_coordinator import ShadowCoordinator, ShadowRunResult
from decoy_engine.execution.physical._shadow_diff_codes import (
    FULL_FRAME_DISPATCH_MISSING_DEPENDENCY,
    FULL_FRAME_RUNTIME_FEATURE_UNSUPPORTED,
    FULL_FRAME_SUBSTRATE_UNSUPPORTED,
    GLOBAL_SHUFFLE_DETERMINISM_UNSUPPORTED,
    GLOBAL_STRATEGY_UNSUPPORTED,
    ShadowDifference,
)
from decoy_engine.execution.physical._shadow_snapshot import capture_shadow_snapshot
from decoy_engine.execution.physical._snapshot import capture_physical_plan_inputs
from decoy_engine.execution.physical._types import DriverId
from decoy_engine.execution.physical.drivers._full_frame import FullFrameAdapter
from decoy_engine.plan._types import Plan
from decoy_engine.relationships import (
    OrphanPolicy,
    RelationshipEdge,
    RelationshipGraph,
    build_namespace_registry,
)
from tests.physical._shadow_helpers import (
    ENGINE_VERSION,
    assert_diagnostics_multisets_equal,
    assert_generation_tables_arrow_ipc_equal,
    build_config,
    write_read_only_fixture,
)

# ---------------------------------------------------------------------------
# Fixtures + harness. The main admitted table uses ONLY the four admitted
# global strategies (item 3): `grp`/`ord_col` are top_code columns with a
# cap far above every value (an effective no-op that still renders a
# canonical numeric string), so `grouped_series` can reference them without
# needing an unadmitted companion strategy (e.g. passthrough) anywhere in
# the table; `derived_aggregate` uses op="count" over `grp` so it needs no
# numeric coercion of an already-masked source column.
# ---------------------------------------------------------------------------

_TABLE = "people"


def _admitted_table_config(
    tmp_path: Path, *, seed: int = 20260917, cap: int = 999_999
) -> tuple[dict[str, Any], pa.Table]:
    columns: list[dict[str, Any]] = [
        {
            "name": "shuffled",
            "strategy": "shuffle",
            "deterministic": True,
            "namespace": "ns_shuffle",
        },
        {
            "name": "grp",
            "strategy": "top_code",
            "provider_config": {"cap": cap, "over_label": "over"},
        },
        {
            "name": "ord_col",
            "strategy": "top_code",
            "provider_config": {"cap": cap, "over_label": "over"},
        },
        {
            "name": "series",
            "strategy": "grouped_series",
            "provider_config": {"group_by": "grp", "order_by": "ord_col", "generator": "cumcount"},
        },
        {
            "name": "agg_out",
            "strategy": "derived_aggregate",
            "provider_config": {"op": "count", "column": "grp"},
        },
    ]
    source = pa.table(
        {
            "shuffled": pa.array(["a", "b", "c", "d", "e", "f"], type=pa.string()),
            "grp": pa.array([0, 0, 1, 1, 2, 2], type=pa.int64()),
            "ord_col": pa.array([2, 1, 2, 1, 2, 1], type=pa.int64()),
            "series": pa.array([0, 0, 0, 0, 0, 0], type=pa.int64()),
            "agg_out": pa.array([0, 0, 0, 0, 0, 0], type=pa.int64()),
        }
    )
    path = write_read_only_fixture(tmp_path, source, _TABLE)
    config = build_config(tmp_path, _TABLE, path, columns, seed=seed)
    return config, source


def _capture(config: dict[str, Any], sources: dict[str, pa.Table]) -> tuple[Any, PhysicalPlan]:
    """`capture_physical_plan_inputs` + `compile_physical_plan`, forced to
    FULL_FRAME (A7): `execution_mode="full_frame"` + `auto_chunk=False` +
    `substrate="pandas"`."""
    inputs = capture_physical_plan_inputs(
        config,
        sources,
        engine_version=ENGINE_VERSION,
        execution_mode="full_frame",
        auto_chunk=False,
        substrate="pandas",
    )
    plan = compile_physical_plan(inputs)
    return inputs, plan


def _build_ctx(
    inputs: Any,
    config: dict[str, Any],
    *,
    plan_override: Plan | None = None,
    relationship_graph_override: RelationshipGraph | None = None,
    adapter: ExecutionAdapter | None = None,
    validators: Any = None,
    quarantine_config: Any = None,
    sink: Any = None,
    source_loader: Any = None,
    vault_writer: Any = None,
    fidelity_report: bool = False,
) -> ShadowContext:
    ns_registry = build_namespace_registry(config, inputs.profile)
    projection_policy = resolve_unconfigured_column_policy(config)
    resolved_adapter = (
        adapter if adapter is not None else select_execution_adapter(substrate="pandas")
    )
    return ShadowContext.from_key_provider(
        plan=plan_override if plan_override is not None else inputs.plan,
        key_provider=None,
        relationship_graph=(
            relationship_graph_override if relationship_graph_override is not None else inputs.graph
        ),
        namespace_registry=ns_registry,
        unconfigured_column_policy=projection_policy,
        full_frame_adapter=resolved_adapter,
        validators=validators,
        quarantine_config=quarantine_config,
        sink=sink,
        source_loader=source_loader,
        vault_writer=vault_writer,
        fidelity_report=fidelity_report,
    )


def _direct_full_frame_result(
    inputs: Any, sources: dict[str, pa.Table], ctx: ShadowContext
) -> ExecutionResult:
    """The A1/A2b comparator: a raw `FullFrameAdapter(...).run()` call over
    the SAME injected adapter + runtime context `dispatch_full_frame` uses,
    over the SAME full source mapping (never narrowed)."""
    assert ctx.full_frame_adapter is not None
    assert ctx.plan is not None
    assert ctx.relationship_graph is not None
    assert ctx.namespace_registry is not None
    driver = FullFrameAdapter(ctx.full_frame_adapter)
    return driver.run(
        ctx.plan,
        dict(sources),
        registry=inputs.registry,
        relationship_graph=ctx.relationship_graph,
        namespace_registry=ctx.namespace_registry,
        unconfigured_column_policy=ctx.unconfigured_column_policy,
        key_provider=ctx.key_provider,
    )


def _assert_outputs_and_diagnostics_equal(
    shadow: ShadowRunResult | ExecutionResult, other: ShadowRunResult | ExecutionResult
) -> None:
    assert_generation_tables_arrow_ipc_equal(dict(shadow.outputs), dict(other.outputs))
    assert_diagnostics_multisets_equal(tuple(shadow.warnings), tuple(other.warnings), "warnings")
    assert_diagnostics_multisets_equal(
        tuple(shadow.row_errors), tuple(other.row_errors), "row_errors"
    )


@dataclasses.dataclass
class _AdapterSpy:
    """A5/A6's zero-invocation proof: an `ExecutionAdapter` test double whose
    `run` fails loudly if ever called. `adapter_name` defaults to "pandas"
    so it passes the substrate gate on its own (a decline must be caused by
    the check under test, not an incidental substrate mismatch); the
    polars-selected test overrides it."""

    adapter_name: str = "pandas"
    adapter_version: str = "test-spy"
    calls: int = 0

    def run(self, plan: Any, sources: Any, **kwargs: Any) -> ExecutionResult:
        self.calls += 1
        raise AssertionError("FullFrameAdapter.run must not be invoked for a declined dispatch")

    def supports_strategy(self, strategy_name: str) -> bool:
        return True

    def shutdown(self) -> None:
        pass


@dataclasses.dataclass
class _SentinelAdapter:
    """A3's forwarding proof: returns a FIXED `ExecutionResult` so the
    coordinator's adaptation step can be asserted to forward `quality_
    metrics` (and everything else) unchanged, unlike a real strategy
    handler here, which never populates `quality_metrics` at all."""

    result: ExecutionResult
    adapter_name: str = "pandas"
    adapter_version: str = "test-sentinel"

    def run(self, plan: Any, sources: Any, **kwargs: Any) -> ExecutionResult:
        return self.result

    def supports_strategy(self, strategy_name: str) -> bool:
        return True

    def shutdown(self) -> None:
        pass


def _with_node_strategy(plan: PhysicalPlan, table: str, column: str, strategy: str) -> PhysicalPlan:
    """Swap ONE work node's `.strategy` on an already-compiled, already-
    admitted plan -- a precise way to exercise the strategy-admission gate
    (item 3) against an unadmitted strategy NAME without needing a fully
    valid compile-time config for it (formula/derived/geo_generalize/
    nested/joint_mask each need their own provider_config shape; the
    admission check only inspects the strategy string)."""
    tables = []
    for physical_table in plan.tables:
        if physical_table.table != table:
            tables.append(physical_table)
            continue
        nodes = tuple(
            dataclasses.replace(node, strategy=strategy) if node.columns == (column,) else node
            for node in physical_table.nodes
        )
        tables.append(dataclasses.replace(physical_table, nodes=nodes))
    return dataclasses.replace(plan, tables=tuple(tables))


def _with_column_seed(live_plan: Plan, table: str, column: str, **replacements: Any) -> Plan:
    """Mutate ONE column's live `ColumnSeed` (e.g. `deterministic`/
    `namespace`) on a copy of the compiled `Plan` -- proves the admission
    gate reads determinism from the LIVE plan, not the compiled
    `PhysicalNode` (which does not retain it, `_plan.py:127`)."""
    new_per_table = []
    for name, table_seed in live_plan.seed_envelope.per_table:
        if name != table:
            new_per_table.append((name, table_seed))
            continue
        new_per_column = tuple(
            (col_name, dataclasses.replace(seed, **replacements) if col_name == column else seed)
            for col_name, seed in table_seed.per_column
        )
        new_per_table.append((name, dataclasses.replace(table_seed, per_column=new_per_column)))
    new_envelope = dataclasses.replace(live_plan.seed_envelope, per_table=tuple(new_per_table))
    return dataclasses.replace(live_plan, seed_envelope=new_envelope)


def _coordinator(inputs: Any, ctx: ShadowContext) -> ShadowCoordinator:
    return ShadowCoordinator(ctx=ctx, registry=inputs.registry)


def _assert_declines(
    inputs: Any, plan: PhysicalPlan, ctx: ShadowContext, snapshot: Any, expected_code: str
) -> None:
    with pytest.raises(ShadowDifference) as excinfo:
        _coordinator(inputs, ctx).run(plan, snapshot)
    assert excinfo.value.code == expected_code
    spy = ctx.full_frame_adapter
    assert isinstance(spy, _AdapterSpy)
    assert spy.calls == 0


# ---------------------------------------------------------------------------
# (a) A1/A7: direct-adapter fidelity, driver forcing.
# ---------------------------------------------------------------------------


def test_direct_adapter_fidelity_for_every_admitted_strategy(tmp_path: Path) -> None:
    """A1 (primary): the coordinator's dispatch is a lossless wrap -- its
    `ShadowRunResult` matches a DIRECT `FullFrameAdapter(...).run()` call
    byte-for-byte (outputs, warnings, row_errors, quality_metrics). A7: the
    compiled plan's driver is forced to and asserted as exactly
    `{FULL_FRAME}`."""
    config, source = _admitted_table_config(tmp_path)
    sources = {_TABLE: source}
    inputs, plan = _capture(config, sources)
    assert {t.driver for t in plan.tables} == {DriverId.FULL_FRAME}

    ctx = _build_ctx(inputs, config)
    snapshot = capture_shadow_snapshot(sources)
    shadow_result = _coordinator(inputs, ctx).run(plan, snapshot)
    direct_result = _direct_full_frame_result(inputs, sources, ctx)

    _assert_outputs_and_diagnostics_equal(shadow_result, direct_result)
    assert shadow_result.quality_metrics == direct_result.quality_metrics
    assert shadow_result.driver_invocation is not None
    assert shadow_result.driver_invocation.driver_id == DriverId.FULL_FRAME
    assert shadow_result.route_evidence == {}


# ---------------------------------------------------------------------------
# (b) A2/A2b: oracle parity.
# ---------------------------------------------------------------------------


def test_oracle_parity_outputs_warnings_row_errors(tmp_path: Path) -> None:
    """A2: outputs/warnings/row_errors match `run_pipeline`'s oracle result,
    accounting for pipeline-added finalization telemetry (never compared:
    timings/table_kinds/execution quality_metrics -- those are added AFTER
    the adapter, `_pipeline.py:566,628`, and `ShadowRunResult` carries none
    of them)."""
    config, source = _admitted_table_config(tmp_path)
    sources = {_TABLE: source}
    inputs, plan = _capture(config, sources)
    ctx = _build_ctx(inputs, config)
    snapshot = capture_shadow_snapshot(sources)
    shadow_result = _coordinator(inputs, ctx).run(plan, snapshot)

    oracle_result = run_pipeline(
        config,
        sources,
        engine_version=ENGINE_VERSION,
        substrate="pandas",
        execution_mode="full_frame",
        auto_chunk=False,
        key_provider=None,
        sink=None,
    )
    assert_generation_tables_arrow_ipc_equal(
        dict(shadow_result.outputs), dict(oracle_result.outputs)
    )
    assert_diagnostics_multisets_equal(
        tuple(shadow_result.warnings), tuple(oracle_result.warnings), "warnings"
    )
    assert_diagnostics_multisets_equal(
        tuple(shadow_result.row_errors), tuple(oracle_result.row_errors), "row_errors"
    )


def test_extra_resident_source_preserved_and_projection_warning_parity(tmp_path: Path) -> None:
    """A2b: an admitted job's snapshot carries an EXTRA resident source
    table beyond the one masked -- the coordinator's output preserves the
    echoed extra-source key AND matches the oracle's projection-warning
    parity. The direct-adapter comparator (A1) uses the SAME full mapping."""
    config, source = _admitted_table_config(tmp_path)
    extra = pa.table({"note": pa.array(["x", "y"], type=pa.string())})
    sources = {_TABLE: source, "extra": extra}
    inputs, plan = _capture(config, sources)
    ctx = _build_ctx(inputs, config)
    snapshot = capture_shadow_snapshot(sources)
    shadow_result = _coordinator(inputs, ctx).run(plan, snapshot)

    assert "extra" in shadow_result.outputs
    assert shadow_result.outputs["extra"].column("note").to_pylist() == ["x", "y"]

    direct_result = _direct_full_frame_result(inputs, sources, ctx)
    _assert_outputs_and_diagnostics_equal(shadow_result, direct_result)

    oracle_result = run_pipeline(
        config,
        sources,
        engine_version=ENGINE_VERSION,
        substrate="pandas",
        execution_mode="full_frame",
        auto_chunk=False,
        key_provider=None,
        sink=None,
    )
    assert_generation_tables_arrow_ipc_equal(
        dict(shadow_result.outputs), dict(oracle_result.outputs)
    )
    assert_diagnostics_multisets_equal(
        tuple(shadow_result.warnings), tuple(oracle_result.warnings), "warnings"
    )


# ---------------------------------------------------------------------------
# (c) A3: quality_metrics forwarding (sentinel test double -- a real
# strategy here emits none, so a real fixture would be a vacuous check).
# ---------------------------------------------------------------------------


def test_quality_metrics_forwarded_unchanged_via_sentinel_adapter(tmp_path: Path) -> None:
    config, source = _admitted_table_config(tmp_path)
    sources = {_TABLE: source}
    inputs, plan = _capture(config, sources)

    sentinel_outputs = {_TABLE: source}
    sentinel = ExecutionResult(
        outputs=sentinel_outputs,
        quality_metrics={"sentinel": "abc123"},
    )
    ctx = _build_ctx(inputs, config, adapter=_SentinelAdapter(result=sentinel))
    snapshot = capture_shadow_snapshot(sources)
    shadow_result = _coordinator(inputs, ctx).run(plan, snapshot)

    assert shadow_result.quality_metrics == {"sentinel": "abc123"}
    assert shadow_result.outputs == sentinel_outputs
    assert shadow_result.warnings == ()
    assert shadow_result.row_errors == ()


# ---------------------------------------------------------------------------
# (d) A4: top_code diagnostics parity (warning + malformed-value RowError).
# ---------------------------------------------------------------------------


def test_top_code_warning_and_row_error_parity(tmp_path: Path) -> None:
    """A4: a fixed-cap top_code job producing a `top_code_generalized`
    WARNING (one value over cap) and a malformed value producing a
    `RowErrorRecord` (one non-numeric string) -- both parity-checked
    against the DIRECT-adapter comparator (A1), not the `run_pipeline`
    oracle: an uncovered row error is job-FATAL there (`_pipeline_finalize.
    finalize_validators_and_quarantine` raises `RowErrorsFailedError`
    without quarantine configured, item 5's own exclusion), while a raw
    `ExecutionResult` merely records it -- exactly the adaptation-fidelity
    claim A1 already proves, applied to a fixture that actually produces
    diagnostics (no percentile cap exists to test)."""
    columns = [
        {
            "name": "age",
            "strategy": "top_code",
            "provider_config": {"cap": 89, "over_label": "90+"},
        }
    ]
    source = pa.table({"age": pa.array(["45", "92", "oops"], type=pa.string())})
    path = write_read_only_fixture(tmp_path, source, "ages")
    config = build_config(tmp_path, "ages", path, columns)
    sources = {"ages": source}
    inputs, plan = _capture(config, sources)
    assert {t.driver for t in plan.tables} == {DriverId.FULL_FRAME}
    ctx = _build_ctx(inputs, config)
    snapshot = capture_shadow_snapshot(sources)
    shadow_result = _coordinator(inputs, ctx).run(plan, snapshot)
    direct_result = _direct_full_frame_result(inputs, sources, ctx)

    _assert_outputs_and_diagnostics_equal(shadow_result, direct_result)
    assert len(direct_result.warnings) == 1
    assert direct_result.warnings[0].code == "top_code_generalized"
    assert len(direct_result.row_errors) == 1


# ---------------------------------------------------------------------------
# (e) A5: deterministic-shuffle cross-run byte identity.
# ---------------------------------------------------------------------------


def test_deterministic_shuffle_cross_run_byte_identical(tmp_path: Path) -> None:
    config, source = _admitted_table_config(tmp_path)
    sources = {_TABLE: source}
    inputs, plan = _capture(config, sources)
    ctx = _build_ctx(inputs, config)
    snapshot = capture_shadow_snapshot(sources)

    first = _coordinator(inputs, ctx).run(plan, snapshot)
    second = _coordinator(inputs, ctx).run(plan, snapshot)
    assert_generation_tables_arrow_ipc_equal(dict(first.outputs), dict(second.outputs))


# ---------------------------------------------------------------------------
# (f) A6: declines, each proven by a zero-invocation adapter spy.
# ---------------------------------------------------------------------------


def test_decline_shuffle_non_deterministic(tmp_path: Path) -> None:
    config, source = _admitted_table_config(tmp_path)
    sources = {_TABLE: source}
    inputs, plan = _capture(config, sources)
    mutated_plan = _with_column_seed(inputs.plan, _TABLE, "shuffled", deterministic=False)
    ctx = _build_ctx(inputs, config, plan_override=mutated_plan, adapter=_AdapterSpy())
    snapshot = capture_shadow_snapshot(sources)
    _assert_declines(inputs, plan, ctx, snapshot, GLOBAL_SHUFFLE_DETERMINISM_UNSUPPORTED)


def test_decline_shuffle_no_namespace(tmp_path: Path) -> None:
    config, source = _admitted_table_config(tmp_path)
    sources = {_TABLE: source}
    inputs, plan = _capture(config, sources)
    mutated_plan = _with_column_seed(inputs.plan, _TABLE, "shuffled", namespace=None)
    ctx = _build_ctx(inputs, config, plan_override=mutated_plan, adapter=_AdapterSpy())
    snapshot = capture_shadow_snapshot(sources)
    _assert_declines(inputs, plan, ctx, snapshot, GLOBAL_SHUFFLE_DETERMINISM_UNSUPPORTED)


@pytest.mark.parametrize(
    "strategy", ["formula", "derived", "geo_generalize", "nested", "joint_mask"]
)
def test_decline_unadmitted_strategy(tmp_path: Path, strategy: str) -> None:
    """Each Phase-5-deferred strategy declines by NAME alone -- the
    admission check only inspects `PhysicalNode.strategy`, so mutating a
    real admitted node's strategy string exercises it precisely without
    needing a valid provider_config for each exotic strategy."""
    config, source = _admitted_table_config(tmp_path)
    sources = {_TABLE: source}
    inputs, plan = _capture(config, sources)
    mutated = _with_node_strategy(plan, _TABLE, "shuffled", strategy)
    ctx = _build_ctx(inputs, config, adapter=_AdapterSpy())
    snapshot = capture_shadow_snapshot(sources)
    _assert_declines(inputs, mutated, ctx, snapshot, GLOBAL_STRATEGY_UNSUPPORTED)


def test_decline_mixed_admitted_and_unadmitted_strategies(tmp_path: Path) -> None:
    config, source = _admitted_table_config(tmp_path)
    sources = {_TABLE: source}
    inputs, plan = _capture(config, sources)
    # `grp` stays top_code (admitted); `shuffled` alone flips to formula --
    # the whole table declines rather than partially masking.
    mutated = _with_node_strategy(plan, _TABLE, "shuffled", "formula")
    ctx = _build_ctx(inputs, config, adapter=_AdapterSpy())
    snapshot = capture_shadow_snapshot(sources)
    _assert_declines(inputs, mutated, ctx, snapshot, GLOBAL_STRATEGY_UNSUPPORTED)


def test_decline_polars_selected_adapter(tmp_path: Path) -> None:
    """A6: the injected adapter's own `adapter_name` (its true runtime
    identity) is checked independently of the compiled `PhysicalTable.
    substrate` -- an adapter claiming "polars" declines even though the
    plan compiled against the pandas default."""
    config, source = _admitted_table_config(tmp_path)
    sources = {_TABLE: source}
    inputs, plan = _capture(config, sources)
    assert plan.tables[0].substrate == "pandas"
    ctx = _build_ctx(inputs, config, adapter=_AdapterSpy(adapter_name="polars"))
    snapshot = capture_shadow_snapshot(sources)
    _assert_declines(inputs, plan, ctx, snapshot, FULL_FRAME_SUBSTRATE_UNSUPPORTED)


def test_decline_relationship_edge_touches_admitted_table(tmp_path: Path) -> None:
    """A6: item 5's FK half. Item 1 (exactly one mask table) already rules
    out the ordinary parent+child shape, so this injects a synthetic
    SELF-referencing edge naming the one admitted table -- a forced
    `execution_mode="full_frame"` can compile such a job onto driver
    FULL_FRAME, so the check must be explicit rather than assumed."""
    config, source = _admitted_table_config(tmp_path)
    sources = {_TABLE: source}
    inputs, plan = _capture(config, sources)
    edge = RelationshipEdge(
        parent_table=_TABLE,
        parent_columns=("grp",),
        child_table=_TABLE,
        child_columns=("grp",),
        namespace="ns_self",
        orphan_policy=OrphanPolicy.PRESERVE,
    )
    graph = RelationshipGraph(edges=(edge,), ordering=((_TABLE, ("grp",)),))
    ctx = _build_ctx(inputs, config, relationship_graph_override=graph, adapter=_AdapterSpy())
    snapshot = capture_shadow_snapshot(sources)
    _assert_declines(inputs, plan, ctx, snapshot, FULL_FRAME_RUNTIME_FEATURE_UNSUPPORTED)


@pytest.mark.parametrize(
    ("kwarg", "value"),
    [
        ("validators", [{"type": "row_count"}]),
        ("quarantine_config", {"path": "q.parquet"}),
        ("sink", object()),
        ("source_loader", lambda table: pa.table({})),
        ("vault_writer", object()),
        ("fidelity_report", True),
    ],
)
def test_decline_runtime_feature_outside_forwarded_contract(
    tmp_path: Path, kwarg: str, value: Any
) -> None:
    config, source = _admitted_table_config(tmp_path)
    sources = {_TABLE: source}
    inputs, plan = _capture(config, sources)
    ctx = _build_ctx(inputs, config, adapter=_AdapterSpy(), **{kwarg: value})
    snapshot = capture_shadow_snapshot(sources)
    _assert_declines(inputs, plan, ctx, snapshot, FULL_FRAME_RUNTIME_FEATURE_UNSUPPORTED)


def test_decline_missing_runtime_dependency(tmp_path: Path) -> None:
    """A6/total-guard: an unset `ShadowContext.full_frame_adapter` (the
    back-compat default every pre-slice-6 caller leaves at `None`) declines
    coded rather than reaching a bare `None.run(...)` AttributeError."""
    config, source = _admitted_table_config(tmp_path)
    sources = {_TABLE: source}
    inputs, plan = _capture(config, sources)
    ns_registry = build_namespace_registry(config, inputs.profile)
    ctx = ShadowContext.from_key_provider(
        plan=inputs.plan,
        key_provider=None,
        relationship_graph=inputs.graph,
        namespace_registry=ns_registry,
        unconfigured_column_policy=resolve_unconfigured_column_policy(config),
        full_frame_adapter=None,
    )
    snapshot = capture_shadow_snapshot(sources)
    with pytest.raises(ShadowDifference) as excinfo:
        _coordinator(inputs, ctx).run(plan, snapshot)
    assert excinfo.value.code == FULL_FRAME_DISPATCH_MISSING_DEPENDENCY


# ---------------------------------------------------------------------------
# (f, continued) A8/A9: inertness/seam + behavior-preservation pins.
# ---------------------------------------------------------------------------


def test_dispatch_full_frame_signature_carries_no_sink_shaped_parameter() -> None:
    """A8: structural proof the FULL_FRAME dispatch cannot publish -- no
    parameter literally named `sink`/`publisher`/`target` anywhere on the
    entry point, mirroring `ShadowCoordinator`'s own C1/C7 guarantee."""
    import inspect

    from decoy_engine.execution.physical._shadow_full_frame import (
        dispatch_full_frame_if_applicable,
    )

    names = set(inspect.signature(dispatch_full_frame_if_applicable).parameters)
    assert names.isdisjoint({"sink", "publisher", "target"})


def test_shadow_coordinator_module_stays_under_the_loc_cap() -> None:
    """A9 (also enforced generally by tests/sentry/test_module_size.py):
    _shadow_coordinator.py stays under the 600-LOC orchestration cap even
    with the FULL_FRAME branch wired in."""
    from decoy_engine.execution.physical import _shadow_coordinator as coordinator_module

    module_path = Path(coordinator_module.__file__)
    loc = sum(1 for _ in module_path.read_text().splitlines())
    assert loc < 600, f"_shadow_coordinator.py is {loc} LOC, over the 600-LOC cap"

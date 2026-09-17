"""Task 4.6 slice 6: the coordinator's FULL_FRAME dispatch -- a compiled plan
whose mask-table driver set is exactly `{DriverId.FULL_FRAME}` wraps the
EXISTING Task 4.2 `FullFrameAdapter` (drivers/_full_frame.py) instead of
running the coordinator's own per-node loop. Split out of
`_shadow_coordinator.py` (which would otherwise exceed the 600-LOC
orchestration cap), mirroring how slice 3's OOC dispatch and slice 5a's
synthesis dispatch each got their own module. This is the LAST masking
slice of the Task 4.6 program; the Phase-5 hard-tail native ports stay
deferred, and this dispatch stays DORMANT (never wired into production
activation -- a full-frame wrap has no per-node native evidence for
`_unified_slice.py`'s contract; that is a separate design).

Scope: the deterministic GLOBAL strategies (`native/_capabilities.py`'s
`is_global=True` set, minus `formula`, which stays deferred) -- shuffle,
top_code, grouped_series, derived_aggregate. "Global" does not mean "always
full_frame" (`top_code` is CHUNK_SAFE and the planner can route an eligible
table to auto-chunk instead), so admission keys on the COMPILED DRIVER being
exactly `{FULL_FRAME}`, never on a strategy name alone (Codex plan-gate
round 1).

Reuse, never re-add: `FullFrameAdapter` already exists and already delegates,
unchanged, to whichever `ExecutionAdapter` `select_execution_adapter` chose
(pandas or polars) -- see its own module docstring. This module never
constructs a `PandasExecutionAdapter` itself; the caller injects the
already-selected instance via `ShadowContext.full_frame_adapter` (Codex
plan-gate: the coordinator must not select or construct the delegate),
mirroring how `ShadowCoordinator.registry` and `ShadowContext.key_provider`
already carry the oracle's own resolved facts for earlier slices.

The source mapping (Codex round 2 HIGH): `dispatch_full_frame` passes the
FULL resident source mapping (`dict(snapshot.tables)`) to `FullFrameAdapter.
run`, never a narrowed single-table mapping. The oracle (`_pipeline.py:511,
534`) invokes the selected adapter over the WHOLE `merged_sources` dict, and
the adapter echoes every supplied source table back into `outputs` (plus any
projection warnings for an extra, unmasked source) -- narrowing the mapping
here would silently drop that echo and diverge from the oracle. With no
generation stage and no code_set in scope (this slice's admission gate rules
out both), `merged_sources == resident_sources == snapshot.tables`, so
`dict(snapshot.tables)` IS exactly the oracle's own adapter input, not an
approximation of it. INVARIANT: every OTHER `FullFrameAdapter.run` parameter
takes its pipeline DEFAULT under this scope -- `pool_cache=None`,
`generate_output_tables=frozenset()`, `row_offset=0`, `code_set_records=
None` (`_full_frame.py:47`) -- so this dispatch never passes them.

Admission (whole-plan; declines coded, never a raw exception -- every check
below is total-guarded):
  1. exactly one mask table (the coordinator's own branch condition already
     proves the driver SET is `{FULL_FRAME}`; this module additionally
     requires exactly one table -- a job-level driver assignment can in
     principle share FULL_FRAME across several independent tables, but this
     slice stays scoped to the single-table case);
  2. the substrate is PANDAS on BOTH signals -- the compiled `PhysicalTable.
     substrate` (a plan-compile-time fact) and the INJECTED adapter's own
     `adapter_name` (its true runtime identity) -- so a caller that compiled
     against one substrate but injected an adapter for the other cannot
     silently diverge from either signal (Codex round 2);
  3. every work node's strategy is in the admitted global set (`shuffle`,
     `top_code`, `grouped_series`, `derived_aggregate`);
  4. DETERMINISM read from the LIVE PLAN's seed envelope, never inferred
     from the strategy name (`PhysicalNode` does not retain `deterministic`,
     `_plan.py:127`): a `shuffle` node's `ColumnSeed` must have
     `deterministic=True` AND a non-empty `namespace` (checked here, rather
     than letting the delegate raise `shuffle_requires_namespace` mid-run);
     `top_code`/`grouped_series`/`derived_aggregate` are deterministic under
     their own handler semantics (config-derived, or job-seed-keyed with no
     dependence on row order) and need no live-plan gate beyond item 3;
  5. no relationship edge names the admitted table, and no runtime feature
     outside the forwarded-call contract: `ShadowContext` mirrors the
     harness's own oracle call, presence-only, for sink/source_loader/
     vault_writer/fidelity_report/validators/quarantine --
     `finalize_validators_and_quarantine` (`_pipeline.py:618`) can FILTER the
     oracle's outputs post-adapter, which a raw `FullFrameAdapter.run()`
     result would never reproduce, so any of these seven declines the whole
     job. Item 1 (exactly one mask table) already rules out the ordinary
     parent+child FK shape, but a forced `execution_mode="full_frame"` can
     still compile a relationship-bearing job onto driver FULL_FRAME (e.g. a
     self-referencing edge within one table), so the FK check is explicit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from decoy_engine.execution.physical._shadow_diff_codes import (
    FULL_FRAME_DISPATCH_MISSING_DEPENDENCY,
    FULL_FRAME_DRIVER_UNSUPPORTED,
    FULL_FRAME_RUNTIME_FEATURE_UNSUPPORTED,
    FULL_FRAME_SUBSTRATE_UNSUPPORTED,
    GLOBAL_SHUFFLE_DETERMINISM_UNSUPPORTED,
    GLOBAL_STRATEGY_UNSUPPORTED,
    ShadowDifference,
)

if TYPE_CHECKING:
    from decoy_engine.execution._adapter import ExecutionAdapter, ExecutionResult
    from decoy_engine.execution.physical._context import SeamContext
    from decoy_engine.execution.physical._plan import PhysicalPlan, PhysicalTable
    from decoy_engine.execution.physical._shadow_context import ShadowContext
    from decoy_engine.execution.physical._shadow_coordinator import (
        ShadowCoordinator,
        ShadowRunResult,
    )
    from decoy_engine.execution.physical._shadow_snapshot import ShadowSnapshot
    from decoy_engine.plan._types import ColumnSeed, Plan
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import NamespaceRegistry, RelationshipGraph

__all__ = ["dispatch_full_frame_if_applicable"]

# Item 3: the deterministic GLOBAL strategy set this slice admits
# (native/_capabilities.py's is_global=True set, minus `formula`, which
# stays deferred -- see the module docstring's Scope paragraph).
ADMITTED_GLOBAL_STRATEGIES = frozenset(
    {"shuffle", "top_code", "grouped_series", "derived_aggregate"}
)


def dispatch_full_frame_if_applicable(
    coordinator: ShadowCoordinator, plan: PhysicalPlan, snapshot: ShadowSnapshot
) -> ShadowRunResult | None:
    """The coordinator's entry point for a `{FULL_FRAME}`-driver plan.
    `None` means "not this dispatch's concern, fall through to the per-node
    loop unchanged" -- a passthrough/redact/truncate/hash/faker-only plan
    (slices 1-4's native-admitted set, disjoint from `ADMITTED_GLOBAL_
    STRATEGIES`) must never be captured here just because its driver
    happens to be FULL_FRAME too. Any OTHER plan (at least one admitted-
    global-strategy node present) commits to this dispatch's admission
    gate below -- decline coded rather than silently falling through, so a
    genuinely mixed table never masks only its native-admitted half."""
    has_candidate_node = any(
        node.strategy in ADMITTED_GLOBAL_STRATEGIES for table in plan.tables for node in table.nodes
    )
    if not has_candidate_node:
        return None
    return _dispatch_full_frame(coordinator, plan, snapshot)


def _dispatch_full_frame(
    coordinator: ShadowCoordinator, plan: PhysicalPlan, snapshot: ShadowSnapshot
) -> ShadowRunResult:
    """Admit + dispatch a `{FULL_FRAME}` plan: wrap the injected,
    already-selected adapter in `FullFrameAdapter` and adapt its
    `ExecutionResult` into a `ShadowRunResult`. See the module docstring for
    the full admission contract."""
    # Imported here, not at module scope: a second reach into `drivers/`
    # from `execution.physical` (the OOC dispatch is the first), kept lazy
    # so a caller that never takes this branch never pays for the import.
    from decoy_engine.execution.physical.drivers._full_frame import FullFrameAdapter

    ctx = coordinator.ctx
    table = _require_single_full_frame_table(plan)
    registry, relationship_graph, namespace_registry, adapter, live_plan = _require_full_frame_deps(
        coordinator, ctx
    )
    _require_admitted_substrate(table, adapter)
    _require_admitted_global_strategies(live_plan, table)
    _require_no_admitted_table_relationship_edges(relationship_graph, table.table)
    _require_no_disqualifying_runtime_features(ctx)

    driver = FullFrameAdapter(adapter)
    result = driver.run(
        live_plan,
        # The FULL resident source mapping, never narrowed -- see the module
        # docstring's "source mapping" section for why this is exactly the
        # oracle's own adapter input under this slice's no-generation scope.
        # Every OTHER parameter below takes FullFrameAdapter's own pipeline
        # default (pool_cache/generate_output_tables/row_offset/
        # code_set_records) rather than being passed explicitly.
        dict(snapshot.tables),
        registry=registry,
        relationship_graph=relationship_graph,
        namespace_registry=namespace_registry,
        unconfigured_column_policy=ctx.unconfigured_column_policy,
        key_provider=ctx.key_provider,
    )
    seam_context = driver.last_invocation
    if seam_context is None:  # pragma: no cover - set unconditionally before delegation
        raise AssertionError("FullFrameAdapter.run returned without setting last_invocation")
    return _adapt_full_frame_result(result, seam_context)


def _adapt_full_frame_result(
    execution_result: ExecutionResult, seam_context: SeamContext
) -> ShadowRunResult:
    """Pure `ExecutionResult` -> `ShadowRunResult` conversion, mirroring
    slice 3's `_adapt_ooc_result`: the SAME `pa.Table` objects `FullFrame
    Adapter.run` produced pass straight through `outputs`; `route_evidence`
    stays empty (there is no per-node loop on this branch to populate one);
    `warnings`/`row_errors`/`quality_metrics` forward by reference,
    unchanged -- this is the adaptation step A1/A3 prove lossless."""
    from decoy_engine.execution.physical._shadow_coordinator import ShadowRunResult

    return ShadowRunResult(
        outputs=dict(execution_result.outputs),
        route_evidence={},
        warnings=execution_result.warnings,
        row_errors=execution_result.row_errors,
        driver_invocation=seam_context,
        quality_metrics=execution_result.quality_metrics,
    )


def _require_single_full_frame_table(plan: PhysicalPlan) -> PhysicalTable:
    """Item 1's table-count half -- the coordinator's own branch condition
    already proves the driver SET is `{FULL_FRAME}`."""
    if len(plan.tables) != 1:
        raise ShadowDifference(
            code=FULL_FRAME_DRIVER_UNSUPPORTED,
            detail=f"{len(plan.tables)} mask table(s), expected exactly 1",
        )
    return plan.tables[0]


def _require_full_frame_deps(
    coordinator: ShadowCoordinator, ctx: ShadowContext
) -> tuple[ProviderRegistry, RelationshipGraph, NamespaceRegistry, ExecutionAdapter, Plan]:
    """The runtime carriers this dispatch needs, all declared `| None` for
    back-compat with every scalar/chunked/OOC/generation caller that never
    sets them (mirrors `ShadowCoordinator._require_ooc_deps`'s pattern): a
    FULL_FRAME dispatch genuinely requires all five."""
    if coordinator.registry is None:
        raise ShadowDifference(
            code=FULL_FRAME_DISPATCH_MISSING_DEPENDENCY,
            detail="ShadowCoordinator.registry is None",
        )
    if ctx.relationship_graph is None:
        raise ShadowDifference(
            code=FULL_FRAME_DISPATCH_MISSING_DEPENDENCY,
            detail="ShadowContext.relationship_graph is None",
        )
    if ctx.namespace_registry is None:
        raise ShadowDifference(
            code=FULL_FRAME_DISPATCH_MISSING_DEPENDENCY,
            detail="ShadowContext.namespace_registry is None",
        )
    if ctx.full_frame_adapter is None:
        raise ShadowDifference(
            code=FULL_FRAME_DISPATCH_MISSING_DEPENDENCY,
            detail="ShadowContext.full_frame_adapter is None",
        )
    if ctx.plan is None:
        raise ShadowDifference(
            code=FULL_FRAME_DISPATCH_MISSING_DEPENDENCY, detail="ShadowContext.plan is None"
        )
    return (
        coordinator.registry,
        ctx.relationship_graph,
        ctx.namespace_registry,
        ctx.full_frame_adapter,
        ctx.plan,
    )


def _require_admitted_substrate(table: PhysicalTable, adapter: ExecutionAdapter) -> None:
    """Item 2: PANDAS on BOTH the compiled `PhysicalTable.substrate` (a
    plan-compile-time fact) and the INJECTED adapter's own `adapter_name`
    (its true runtime identity) -- checking only one would let the other
    silently diverge (Codex round 2)."""
    if table.substrate != "pandas":
        raise ShadowDifference(
            code=FULL_FRAME_SUBSTRATE_UNSUPPORTED,
            detail=f"compiled table substrate={table.substrate!r}",
        )
    if adapter.adapter_name != "pandas":
        raise ShadowDifference(
            code=FULL_FRAME_SUBSTRATE_UNSUPPORTED,
            detail=f"injected adapter adapter_name={adapter.adapter_name!r}",
        )


def _require_admitted_global_strategies(live_plan: Plan, table: PhysicalTable) -> None:
    """Items 3/4: every work node's strategy is in the admitted global set,
    and a `shuffle` node additionally carries `ColumnSeed.deterministic=True`
    plus a namespace, read from the LIVE plan's seed envelope (never the
    strategy name alone -- `PhysicalNode` does not retain `deterministic`,
    `_plan.py:127`)."""
    seeds = _column_seeds_for_table(live_plan, table.table)
    for node in table.nodes:
        if node.strategy not in ADMITTED_GLOBAL_STRATEGIES:
            raise ShadowDifference(
                code=GLOBAL_STRATEGY_UNSUPPORTED,
                detail=f"{table.table}: strategy {node.strategy!r} not admitted",
            )
        if node.strategy == "shuffle":
            _require_deterministic_namespaced_shuffle(seeds, table.table, node.columns)


def _column_seeds_for_table(live_plan: Plan, table_name: str) -> dict[str, ColumnSeed]:
    """The live plan's per-column `ColumnSeed` map for one table (item 4's
    live-plan source of truth), total-guarded: a malformed `seed_envelope`
    declines coded rather than raising raw out of this admission check."""
    try:
        per_table = dict(live_plan.seed_envelope.per_table)
        table_seed = per_table.get(table_name)
        return dict(table_seed.per_column) if table_seed is not None else {}
    except (AttributeError, TypeError, ValueError) as exc:
        raise ShadowDifference(
            code=GLOBAL_STRATEGY_UNSUPPORTED,
            detail=f"{table_name}: live plan seed_envelope is malformed",
        ) from exc


def _require_deterministic_namespaced_shuffle(
    seeds: dict[str, ColumnSeed], table_name: str, columns: tuple[str, ...]
) -> None:
    if len(columns) != 1:
        raise ShadowDifference(
            code=GLOBAL_STRATEGY_UNSUPPORTED,
            detail=f"{table_name}: shuffle node has {len(columns)} columns, expected 1",
        )
    column_seed = seeds.get(columns[0])
    if column_seed is None:
        raise ShadowDifference(
            code=GLOBAL_STRATEGY_UNSUPPORTED,
            detail=f"{table_name}.{columns[0]}: no live ColumnSeed for shuffle node",
        )
    if not column_seed.deterministic or not column_seed.namespace:
        raise ShadowDifference(
            code=GLOBAL_SHUFFLE_DETERMINISM_UNSUPPORTED,
            detail=f"{table_name}.{columns[0]}: shuffle is not deterministic-and-namespaced",
        )


def _require_no_admitted_table_relationship_edges(
    relationship_graph: RelationshipGraph, table_name: str
) -> None:
    """Item 5's FK half: no relationship edge may name the one admitted
    table, on either side. `item 1` (exactly one mask table) already rules
    out the ordinary parent+child FK shape (which needs two mask tables),
    but a forced `execution_mode="full_frame"` can still compile a
    relationship-bearing job onto driver FULL_FRAME (`_reasons.
    ROUTE_OVERRIDE_FULL_FRAME`) -- e.g. a self-referencing edge within one
    table -- so this is checked explicitly rather than assumed from item 1."""
    try:
        edges = relationship_graph.edges
    except AttributeError as exc:
        raise ShadowDifference(
            code=FULL_FRAME_RUNTIME_FEATURE_UNSUPPORTED,
            detail="ShadowContext.relationship_graph has no edges attribute",
        ) from exc
    for edge in edges:
        if edge.parent_table == table_name or edge.child_table == table_name:
            raise ShadowDifference(
                code=FULL_FRAME_RUNTIME_FEATURE_UNSUPPORTED,
                detail=f"{table_name}: a relationship edge touches the admitted table",
            )


def _require_no_disqualifying_runtime_features(ctx: ShadowContext) -> None:
    """Item 5: no runtime feature outside the forwarded-call contract.
    `finalize_validators_and_quarantine` (`_pipeline.py:618`) can filter the
    oracle's outputs post-adapter; a raw `FullFrameAdapter.run()` result
    never reproduces that, so any of the six declines the whole job."""
    if ctx.sink_requested:
        raise ShadowDifference(
            code=FULL_FRAME_RUNTIME_FEATURE_UNSUPPORTED, detail="ctx.sink_requested"
        )
    if ctx.source_loader_requested:
        raise ShadowDifference(
            code=FULL_FRAME_RUNTIME_FEATURE_UNSUPPORTED, detail="ctx.source_loader_requested"
        )
    if ctx.vault_writer_requested:
        raise ShadowDifference(
            code=FULL_FRAME_RUNTIME_FEATURE_UNSUPPORTED, detail="ctx.vault_writer_requested"
        )
    if ctx.fidelity_report:
        raise ShadowDifference(
            code=FULL_FRAME_RUNTIME_FEATURE_UNSUPPORTED, detail="ctx.fidelity_report"
        )
    if ctx.validators_requested:
        raise ShadowDifference(
            code=FULL_FRAME_RUNTIME_FEATURE_UNSUPPORTED, detail="ctx.validators_requested"
        )
    if ctx.quarantine_requested:
        raise ShadowDifference(
            code=FULL_FRAME_RUNTIME_FEATURE_UNSUPPORTED, detail="ctx.quarantine_requested"
        )

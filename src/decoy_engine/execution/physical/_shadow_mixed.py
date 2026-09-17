"""Task 4.6 slice 5b-i/5b-ii: the coordinator's MIXED dispatch -- a plan with
both generate tables and mask tables, either INDEPENDENT (no `generate-
parent -> mask-child` edge, slice 5b-i) or COUPLED through exactly one
admitted such edge (slice 5b-ii: the mask child's FK parent IS a generate
table, so the mask side must read the generate output as its FK pool). Kept
out of `_shadow_coordinator.py` (which would otherwise exceed the 600-LOC
orchestration cap) and out of `_shadow_generation.py` (whose job stays
scoped to the pure-only contract), mirroring how slice 3's OOC dispatch and
slice 5a's synthesis dispatch each got their own module.

`require_independent_mixed_shadowable` (contract C, plan section "The
eligibility gate"; name kept from 5b-i even though it now also admits the
coupled case, since it is the one gate every caller -- production dispatch
and the test suite's own raw-guard tests -- already imports by this name)
admits iff:

  1. the plan genuinely mixes generate and mask tables (guaranteed by the
     coordinator's own branch condition before this is called; re-asserted
     here as a total guard);
  2. the generate half's shape is admitted (`_shadow_generation.
     require_generation_shape`, the same identity/column-shape core the pure
     gate uses), AND its MATERIALIZED output is round-trip-stable through the
     oracle's pandas echo (`_require_roundtrip_stable_generate_outputs`,
     checked in `dispatch_mixed` AFTER generation, not here -- it declines any
     null, floating NaN, or nested generate output; see that function for the
     full "why". Mixed-specific: the pure path never reaches it);
  3. every mask-table driver is in the already-proven scalar/full_frame/
     chunked admitted set -- OUT_OF_CORE is REJECTED. This is not a future
     capability a later slice lifts: a mixed job always routes full_frame/
     pandas in production (`_pipeline_routing.py`'s `generate_plus_mask`
     never selects `out_of_core`), so an OOC mask driver can never
     legitimately reach this gate from a real compiled plan; the check is a
     defensive total guard, not a scoped-out feature;
  4. the relationship graph's `generate-parent -> mask-child` edges resolve
     to EITHER zero (the independent case, unchanged from 5b-i) OR exactly
     one edge admitted by the complete-graph rule (`_select_admitted_
     coupled_edge`, slice 5b-ii, item 5 below) -- more than one crossing
     edge, or one that fails the rule, declines coded rather than guessing
     which (if any) should win;
  5. COMPLETE GRAPH ADMISSION RULE for the coupled case: the crossing edge's
     parent and child keys are each a single (non-composite) column, of an
     admitted Arrow type (int or string -- the child's type is checked here
     from the resident snapshot; the parent's type is checked in `dispatch_
     mixed` AFTER generation runs, since a generate table's actual column
     type is not known before that); no OTHER relationship edge in the graph
     touches a mask table at all, on either side -- this one condition
     subsumes "no other incoming edge to the child", "no outgoing edge from
     the child or the generated parent reaching a mask table", and "no FK
     edge involving any other mask table" all at once, since the child
     itself is always a mask table, so any other edge naming it (as parent
     or child) or naming any other mask table already fails this check;
  6. no job-level validators, quarantine, vault writer, fidelity reporting,
     or `mask_secret_ref` (the last because `ShadowContext.from_key_
     provider` never threads it -- see `_shadow_mixed.py`'s own runtime-
     contract check below for why leaving it ungated would silently
     diverge the resolved mask key from the oracle's);
  7. no sink or source-loader requested (inertness; the coordinator
     structurally cannot publish or lazily load, so these can only ever be
     `False`, but a caller mirroring a sink/loader into its own oracle call
     must decline here, not silently ignore the mismatch).

`derive_key` / `instance_default_locale` / `key_provider` are NOT gated:
they pass through to `generate_tables` / the mask loop's key material
exactly as the oracle sees them (parity-proven, not admission-checked) --
unlike the pure gate, which declines any of the three since a pure job has
no oracle-side masking to reuse the resolved key for.

`dispatch_mixed` runs generation via the SAME adapter call slice 5a's pure
dispatch uses (`_shadow_generation.run_synthesis_adapter`, over a plan
already admitted by this module's own gate -- never
`require_pure_generation_shadowable`, which would reject the very
sources/relationships/snapshot a real mixed job carries). For the COUPLED
case it then checks the admitted edge's PARENT key type (only now knowable,
against the materialized generate output) and injects `generate_outputs`
into the mask-half's snapshot (`{**snapshot.tables, **generate_outputs}`,
mirroring the oracle's own `merged_sources`, harmless for the independent
case where nothing looks the extra entries up) alongside a run-scoped tuple
of admitted `RelationshipEdge` identities, so the reused per-node loop can
resolve the one allowlisted FK child (`_shadow_fk.py`) while coded-declining
any other FK-child node it encounters, never inferring admission from the
graph alone (`_shadow_coordinator.py`'s own per-node loop). Either way, the
mask half reuses the coordinator's OWN scalar/chunked per-node loop (by
recursing into `ShadowCoordinator.run` with `synthesis` stripped off the
plan), then stitches the two outputs via the one shared precedence helper
(`execution._stitch.stitch_generate_mask_outputs`) both this module and the
oracle (`_pipeline.py`) call. Generation runs first and unconditionally --
matching the oracle's own Step 1/Step 2 order (`_pipeline.py:481-594`) -- so
a generate-side fault propagates before the mask loop is ever reached,
mirroring the oracle's generate-first failure semantics exactly.

The oracle's `finalize_validators_and_quarantine` (SP-05/D8) is never
called from this path: it can WRITE quarantine artifacts, which would
violate shadow inertness (C1/C7). The admission gate above declines any job
carrying validators/quarantine/vault-writer/fidelity-reporting, which
guarantees the oracle's own finalizer is a no-op (it neither filters
outputs nor writes anything) over the domain this gate admits -- so parity
holds without ever invoking it here.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

import pyarrow as pa
import pyarrow.compute as pc

from decoy_engine.execution._stitch import stitch_generate_mask_outputs
from decoy_engine.execution.physical._shadow_diff_codes import (
    GENERATION_SHAPE_UNSUPPORTED,
    MIXED_DRIVER_UNSUPPORTED,
    MIXED_FK_CROSS_GENERATE_UNSUPPORTED,
    MIXED_FK_TOPOLOGY_UNSUPPORTED,
    ShadowDifference,
)
from decoy_engine.execution.physical._shadow_generation import (
    require_generation_shape,
    run_synthesis_adapter,
)
from decoy_engine.execution.physical._shadow_snapshot import capture_shadow_snapshot
from decoy_engine.execution.physical._types import DriverId

if TYPE_CHECKING:
    from decoy_engine.execution.physical._plan import PhysicalPlan
    from decoy_engine.execution.physical._shadow_context import ShadowContext
    from decoy_engine.execution.physical._shadow_coordinator import (
        ShadowCoordinator,
        ShadowRunResult,
    )
    from decoy_engine.execution.physical._shadow_snapshot import ShadowSnapshot
    from decoy_engine.plan._types import Plan
    from decoy_engine.relationships._graph import RelationshipEdge

__all__ = ["dispatch_mixed", "require_independent_mixed_shadowable"]


# Task 4.6 slice 5b-ii admission rule #5's key-shape check: a single-column
# int or string key. Composite keys and every other scalar type (float,
# decimal, bool, timestamp, ...) stay deferred -- see `_shadow_fk.py`'s
# module docstring for why the write-back bridge only needs to reproduce
# these two families.
def _is_admitted_fk_key_type(arrow_type: pa.DataType | None) -> bool:
    return arrow_type is not None and (
        pa.types.is_integer(arrow_type)
        or pa.types.is_string(arrow_type)
        or pa.types.is_large_string(arrow_type)
    )


def dispatch_mixed(
    coordinator: ShadowCoordinator, plan: PhysicalPlan, snapshot: ShadowSnapshot
) -> ShadowRunResult:
    """Admit + dispatch a MIXED plan (independent or coupled): generate first
    (the same adapter path slice 5a uses), then the mask half through the
    coordinator's own reused loop, then stitch. See the module docstring for
    the full admission contract and why each step is safe to reuse
    unmodified."""
    # Imported here, not at module scope: `_shadow_coordinator` imports THIS
    # module (to route the mixed branch), so a module-level import back
    # would be circular. Matches the lazy-import precedent the OOC and
    # synthesis dispatches already set.
    from decoy_engine.execution.physical._shadow_coordinator import ShadowRunResult

    if plan.synthesis is None:  # pragma: no cover - the coordinator's own branch condition
        raise AssertionError("dispatch_mixed called with plan.synthesis is None")
    ctx = coordinator.ctx
    plan_obj, admitted_edge = require_independent_mixed_shadowable(ctx, plan, snapshot)

    generate_outputs, generate_seam = run_synthesis_adapter(ctx, plan_obj, plan.synthesis)
    # Gate on the ACTUAL generated output, not on config knobs: a mixed job's
    # generate output is echoed back through the oracle's pandas mask adapter
    # and wins the Step-3 tie, so a column that is not byte-stable through
    # that round-trip diverges from this dispatch's raw native Arrow. This
    # runs AFTER generation (which already happened, inertly -- nothing is
    # published) precisely so it catches instability from ANY source -- the
    # `null_probability` knob, a `None`/NaN in `categories`, a nested category
    # type -- rather than trying to predict it from the config (whack-a-mole).
    _require_roundtrip_stable_generate_outputs(generate_outputs)

    admitted_edges: tuple[RelationshipEdge, ...] = ()
    if admitted_edge is not None:
        # The parent key's Arrow type is only knowable now, against the
        # materialized generate output (unlike the child's, which the
        # admission gate already checked from the resident snapshot).
        _require_admitted_parent_key_type(admitted_edge, generate_outputs)
        admitted_edges = (admitted_edge,)

    # Mirrors the oracle's own `merged_sources = resident_sources |
    # generate_outputs` (`_pipeline.py`): a mask table whose FK parent is a
    # generate table reads the generate output as its FK pool. Harmless for
    # the independent case (`admitted_edges` empty) -- nothing looks the
    # extra entries up.
    merged_snapshot = capture_shadow_snapshot({**snapshot.tables, **generate_outputs})

    # Reuse `ShadowCoordinator.run` itself for the mask half, UNCHANGED: a
    # plan with `synthesis` stripped off falls straight through this
    # method's own pure-generate and mixed branches (both false now) into
    # the scalar/chunked per-node loop -- the OOC branch is already
    # unreachable here, since the gate above rejected any OUT_OF_CORE
    # driver before this call. This is the "reuse, never reimplement" the
    # plan requires for the mask side, made literal rather than duplicated.
    # `admitted_edges` (default `()`) is the only new signal threaded
    # through: every pre-5b-ii caller of `run()` leaves it at that default.
    mask_only_plan = dataclasses.replace(plan, synthesis=None)
    mask_result = coordinator.run(mask_only_plan, merged_snapshot, admitted_edges=admitted_edges)

    outputs = stitch_generate_mask_outputs(generate_outputs, mask_result.outputs)
    # `driver_invocation` names the generate half's seam (mirroring slice
    # 5a's own pure-dispatch `ShadowRunResult`): the mask half's reused loop
    # never produces one of its own (see `ShadowCoordinator.run`'s final
    # return, which leaves it at the default `None`), so there is only one
    # seam identity worth recording here.
    return ShadowRunResult(
        outputs=outputs,
        route_evidence=mask_result.route_evidence,
        warnings=mask_result.warnings,
        row_errors=mask_result.row_errors,
        driver_invocation=generate_seam,
        quality_metrics=mask_result.quality_metrics,
    )


def require_independent_mixed_shadowable(
    ctx: ShadowContext, plan: PhysicalPlan, snapshot: ShadowSnapshot
) -> tuple[Plan, RelationshipEdge | None]:
    """Contract C -- see the module docstring for the full admitted-domain
    list. Returns the compiled generation `Plan` plus the ONE admitted
    generate-parent -> mask-child edge (`None` for the independent case,
    unchanged from 5b-i).
    """
    if not plan.tables or plan.synthesis is None:  # pragma: no cover - coordinator guards this
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED, detail="plan is not a mixed generate+mask shape"
        )
    plan_obj, config, generate_table_names = require_generation_shape(ctx, plan.synthesis)
    # Note: round-trip stability of the generate OUTPUT, and the admitted
    # edge's PARENT key type, are both checked in `dispatch_mixed` after
    # generation runs, not here -- both are properties of the materialized
    # Arrow tables, not of the pre-generation config/graph shape.

    mask_table_names = _table_name_set(plan)
    _require_admitted_mask_drivers(plan)
    admitted_edge = _select_admitted_coupled_edge(
        ctx, generate_table_names, mask_table_names, snapshot
    )
    _require_no_disqualifying_job_settings(ctx, config)
    return plan_obj, admitted_edge


def _require_roundtrip_stable_generate_outputs(generate_outputs: dict[str, pa.Table]) -> None:
    """Decline a mixed job whose GENERATED output is not byte-stable through
    the oracle's pandas echo.

    In a mixed job the oracle merges `generate_outputs` into `merged_sources`,
    the pandas mask adapter round-trips every source frame through
    `to_pandas`/`from_pandas`, and Step-3 lets that echoed copy WIN the name
    tie -- so the oracle's final generate-table output is pandas-round-tripped
    while this shadow dispatch stitches the RAW native-Arrow output. That
    round-trip is NOT the identity for three column shapes, each of which
    genuinely diverges under the byte comparator:

      * any null (`null_count > 0`): an integer column widens to double
        (pandas has no nullable-int in the boundary path) and null slots
        refill differently;
      * a floating NaN (even with `null_count == 0`): `from_pandas` folds a
        NaN back to an Arrow null on re-import, so shadow (NaN) != oracle
        (null);
      * a nested type (`list`/`struct`/`map`/union): the element/field type
        widens through pandas the same way a top-level numeric does
        (`list<int64>` -> `list<double>`).

    Gating on the MATERIALIZED output rather than the config knobs is
    deliberate (dennis BLOCKER re-review): nullability/instability reaches the
    output through `null_probability`, a `None`/NaN in `categories`, or a
    nested category alike, and only the actual Arrow tables capture all of
    them in one check. The check runs after generation (inert -- nothing is
    published) and declines coded. Non-null, NaN-free, non-nested output of
    any admitted type round-trips identically and is admitted. Nullable/
    unstable generate columns in a mixed job are a tracked exclusion deferred
    to a later slice. The PURE path (slice 5a) never reaches here -- it has no
    mask adapter to echo through, so its nullable columns stay byte-stable.
    """
    for table_name, table in generate_outputs.items():
        for column_name in table.column_names:
            column = table.column(column_name)
            field_type = column.type
            if _is_nested_arrow_type(field_type):
                raise ShadowDifference(
                    code=GENERATION_SHAPE_UNSUPPORTED,
                    detail=(
                        f"table={table_name!r} column={column_name!r}: nested generate output "
                        f"type {field_type} is not round-trip-stable through the oracle's pandas "
                        "echo in a mixed job (deferred)"
                    ),
                )
            if column.null_count > 0:
                raise ShadowDifference(
                    code=GENERATION_SHAPE_UNSUPPORTED,
                    detail=(
                        f"table={table_name!r} column={column_name!r}: a nullable generate output "
                        "is not round-trip-stable through the oracle's pandas echo in a mixed job "
                        "(deferred)"
                    ),
                )
            if pa.types.is_floating(field_type) and _float_column_has_nan(column):
                raise ShadowDifference(
                    code=GENERATION_SHAPE_UNSUPPORTED,
                    detail=(
                        f"table={table_name!r} column={column_name!r}: a floating generate output "
                        "carries NaN, which the oracle's pandas echo folds to null (not "
                        "round-trip-stable in a mixed job; deferred)"
                    ),
                )


def _is_nested_arrow_type(field_type: pa.DataType) -> bool:
    return (
        pa.types.is_list(field_type)
        or pa.types.is_large_list(field_type)
        or pa.types.is_fixed_size_list(field_type)
        or pa.types.is_struct(field_type)
        or pa.types.is_map(field_type)
        or pa.types.is_union(field_type)
    )


def _float_column_has_nan(column: pa.ChunkedArray) -> bool:
    # A NaN is distinct from a null in Arrow, so `null_count` misses it; the
    # oracle's `from_pandas` re-import folds NaN -> null, so a NaN-bearing
    # shadow column diverges from the oracle. `pc.is_nan` is defined only on
    # floating input (guarded by the caller), and returns null for null slots
    # -- `pc.any(..., skip_nulls=True)` treats those as "no NaN here".
    return bool(
        pc.any(pc.is_nan(column), skip_nulls=True).as_py()  # type: ignore[attr-defined, unused-ignore]
    )


def _table_name_set(plan: PhysicalPlan) -> frozenset[str]:
    try:
        return frozenset(table.table for table in plan.tables)
    except TypeError as exc:
        # An unhashable mask-table name declines coded, never raises a raw
        # TypeError out of the frozenset construction.
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED, detail="a mask table name is unhashable"
        ) from exc


def _require_admitted_mask_drivers(plan: PhysicalPlan) -> None:
    """Item 3: every mask-table driver must be in the already-proven
    scalar/full_frame/chunked admitted set -- OUT_OF_CORE declines
    (`MIXED_DRIVER_UNSUPPORTED`, matching the same code the OOC branch's own
    mixed-driver-set decline uses). Checked BEFORE the FK-edge traversal
    below, so an OOC mask driver declines without ever requiring
    `ShadowContext.relationship_graph` to be set."""
    try:
        mask_drivers = frozenset(table.driver for table in plan.tables)
    except TypeError as exc:
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED, detail="a mask table driver is unhashable"
        ) from exc
    if DriverId.OUT_OF_CORE in mask_drivers:
        raise ShadowDifference(
            code=MIXED_DRIVER_UNSUPPORTED, detail="out_of_core mask driver in a mixed dispatch"
        )


def _select_admitted_coupled_edge(
    ctx: ShadowContext,
    generate_table_names: frozenset[str],
    mask_table_names: frozenset[str],
    snapshot: ShadowSnapshot,
) -> RelationshipEdge | None:
    """Item 4/5: find the AT MOST ONE `generate-parent -> mask-child` edge
    this dispatch may couple the mask half's FK resolution to. `None` means
    the independent case (no crossing edge at all -- unchanged 5b-i
    behavior). The reverse direction (a mask-parent referenced by a generate
    child) is already rejected upstream at generation-config validation, so
    it never reaches this gate -- only THIS direction needs checking.

    The admitted edge's PARENT key type is checked separately, in `dispatch_
    mixed`, AFTER generation runs (a generate table's actual column type is
    not knowable from the graph alone)."""
    graph = ctx.relationship_graph
    if graph is None:
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED, detail="ShadowContext.relationship_graph is None"
        )
    try:
        edges = graph.edges
    except AttributeError as exc:
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED,
            detail="ShadowContext.relationship_graph has no edges attribute",
        ) from exc

    crossing: list[RelationshipEdge] = []
    for candidate in edges:
        try:
            parent_table = candidate.parent_table
            child_table = candidate.child_table
        except AttributeError as exc:
            raise ShadowDifference(
                code=GENERATION_SHAPE_UNSUPPORTED, detail="a relationship edge is malformed"
            ) from exc
        try:
            crosses = parent_table in generate_table_names and child_table in mask_table_names
        except TypeError as exc:
            raise ShadowDifference(
                code=GENERATION_SHAPE_UNSUPPORTED,
                detail="a relationship edge table name is unhashable",
            ) from exc
        if crosses:
            crossing.append(candidate)

    if not crossing:
        return None
    if len(crossing) > 1:
        raise ShadowDifference(
            code=MIXED_FK_TOPOLOGY_UNSUPPORTED,
            detail=f"{len(crossing)} generate-parent -> mask-child edges present",
        )
    edge = crossing[0]
    if len(edge.parent_columns) != 1 or len(edge.child_columns) != 1:
        raise ShadowDifference(
            code=MIXED_FK_CROSS_GENERATE_UNSUPPORTED,
            detail=(
                f"parent={edge.parent_table!r} child={edge.child_table!r}: "
                "composite FK key not admitted"
            ),
        )
    child_type = _resident_column_type(snapshot, edge.child_table, edge.child_columns[0])
    if not _is_admitted_fk_key_type(child_type):
        raise ShadowDifference(
            code=MIXED_FK_CROSS_GENERATE_UNSUPPORTED,
            detail=(
                f"child={edge.child_table!r}.{edge.child_columns[0]!r}: "
                f"key type {child_type} not admitted"
            ),
        )
    # The complete-graph rule (plan item 5): no OTHER relationship edge may
    # touch a mask table at all, on either side. Since the admitted edge's
    # own child is always a mask table, this single check subsumes "no other
    # incoming edge to the child", "no outgoing edge from the child or the
    # generated parent reaching a mask table", and "no FK edge involving any
    # other mask table" all at once -- any other edge naming the child (as
    # parent or child) or naming a different mask table fails it.
    for other in edges:
        if other is edge:
            continue
        if other.parent_table in mask_table_names or other.child_table in mask_table_names:
            raise ShadowDifference(
                code=MIXED_FK_TOPOLOGY_UNSUPPORTED,
                detail="another relationship edge touches a mask table",
            )
    return edge


def _resident_column_type(snapshot: ShadowSnapshot, table: str, column: str) -> pa.DataType | None:
    resident = snapshot.tables.get(table)
    if resident is None or column not in resident.schema.names:
        return None
    return resident.schema.field(column).type


def _require_admitted_parent_key_type(
    edge: RelationshipEdge, generate_outputs: dict[str, pa.Table]
) -> None:
    """The other half of admission rule #5's key-shape check, deferred until
    now because a generate table's actual column type is only knowable
    against its materialized output (nothing about `sequence`/`categorical`
    config alone pins int-vs-string, and a mixed-type `categorical` is
    possible in principle)."""
    parent_table = generate_outputs.get(edge.parent_table)
    column_name = edge.parent_columns[0]
    key_type = (
        parent_table.schema.field(column_name).type
        if parent_table is not None and column_name in parent_table.schema.names
        else None
    )
    if not _is_admitted_fk_key_type(key_type):
        raise ShadowDifference(
            code=MIXED_FK_CROSS_GENERATE_UNSUPPORTED,
            detail=(
                f"parent={edge.parent_table!r}.{column_name!r}: key type {key_type} not admitted"
            ),
        )


def _require_no_disqualifying_job_settings(ctx: ShadowContext, config: dict[str, object]) -> None:
    """Items 5-6: no job-level validators/quarantine/vault-writer/fidelity-
    reporting/`mask_secret_ref`, and no sink/source-loader requested.
    `derive_key`/`instance_default_locale`/`key_provider` are deliberately
    NOT checked here -- see the module docstring's "parity-proven, not
    admission-checked" note."""
    if config.get("validators"):
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED, detail="config.validators is non-empty"
        )
    if config.get("quarantine") is not None:
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED, detail="config.quarantine is not None"
        )
    global_settings = config.get("global_settings")
    if isinstance(global_settings, dict) and global_settings.get("mask_secret_ref") is not None:
        # `ShadowContext.from_key_provider` never threads `mask_secret_ref`
        # through to `resolve_mask_key` (`_shadow_context.py`), so a caller
        # relying on the ref-resolution fallback would resolve a DIFFERENT
        # mask key on the shadow side than the oracle's `resolve_key_
        # provider` does -- a real parity gap, not a defensive-only guard.
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED, detail="global_settings.mask_secret_ref is set"
        )
    if ctx.vault_writer_requested:
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED, detail="ctx.vault_writer_requested"
        )
    if ctx.fidelity_report:
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED, detail="ctx.fidelity_report is true"
        )
    if ctx.sink_requested:
        raise ShadowDifference(code=GENERATION_SHAPE_UNSUPPORTED, detail="ctx.sink_requested")
    if ctx.source_loader_requested:
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED, detail="ctx.source_loader_requested"
        )

"""D2: `compile_physical_plan(inputs) -> PhysicalPlan` -- the pure compiler.

Executes nothing; not wired into `run_pipeline` (D5's sentries enforce the
disconnection). The frozen SELECTION PRECEDENCE (TASK-4.3-PLAN.md D2), from
`_pipeline.py`'s call order:

  0. Submit-boundary substrate resolution + adapter validation happen BEFORE
     Layer 1, outside the compiler (`PhysicalPlanInputs.resolved_substrate`
     is already-validated by construction; see D3's validation-code
     exclusion in `_reasons.py`).
  1. Synthesis stage from `Plan.generation` (generate-kind tables, produced
     before masking).
  2. Layer-1 `decide_execution_route` -> `full_frame` / `sequential` /
     `out_of_core`.
  3. `native_stream` admission: if admitted, it RETURNS and PREEMPTS the
     chunk branch (`_pipeline.py:453`); the chunk candidate is computed but
     used only if native is not admitted.
  4. Else Layer-2 chunked-vs-`full_frame`.

Deliberate design choice (see `_inputs.py`'s module docstring): every stage
above calls the LIVE production decision function directly
(`decide_execution_route`, `classify_job`) rather than reimplementing its
branching, so equivalence for what those functions decide is definitional.
What this module owns is strictly the PRECEDENCE wiring between them plus
the two narrowing decisions layer 1/2 do not make on their own (native
preempting chunk; which driver a job's mask tables end up on).

Every function below is a free function (module-level, not a class method),
by design: the repo's mutation tooling (mutmut 3.7) skips decorated-class
bodies, so routing-decision logic that needs mutation coverage has to live
outside one (see `mutmut-decorated-class-limitation` in the profile
guidance the plan cites).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from decoy_engine.execution.physical import _reasons
from decoy_engine.execution.physical._plan import (
    PhysicalNode,
    PhysicalPlan,
    PhysicalTable,
    RejectedAlternative,
    SynthesisStage,
)
from decoy_engine.execution.physical._types import DriverId

if TYPE_CHECKING:
    from decoy_engine.execution._planner import ExecutionPlan
    from decoy_engine.execution.physical._inputs import NativeAdmissionFact, PhysicalPlanInputs
    from decoy_engine.relationships import RelationshipGraph

__all__ = [
    "DriverSelection",
    "compile_physical_plan",
    "layer1_route",
    "layer2_chunk_decision",
    "native_applies",
    "out_of_core_not_ready_reason",
    "select_driver",
]

_NATIVE_ROUTE_NOT_APPLICABLE = "native_route_disabled_or_no_mask_table"
_CHUNK_NOT_APPLICABLE = "auto_chunk_disabled_or_no_mask_table"


@dataclass(frozen=True)
class DriverSelection:
    driver: DriverId
    reason: str
    reason_detail: str | None
    rejected_alternatives: tuple[RejectedAlternative, ...]


def layer1_route(inputs: PhysicalPlanInputs) -> tuple[str, str]:
    """Call `decide_execution_route` (`_pipeline_routing.py`) directly on the
    captured signal values in `inputs.out_of_core_facts` -- the exact
    function `run_pipeline` calls, so this can raise the same `ExecutionError`
    (reject-before-read) or `ConfigError` (forced-mode failure) it does.
    """
    from decoy_engine.execution._pipeline_routing import decide_execution_route

    facts = inputs.out_of_core_facts
    vault_writer_sentinel = object() if inputs.vault_writer_present else None
    return decide_execution_route(
        inputs.profile,
        has_generate_table=inputs.has_generate_table,
        has_mask_table=inputs.has_mask_table,
        validators=list(inputs.validators),
        fidelity_report=inputs.fidelity_report,
        vault_writer=vault_writer_sentinel,
        execution_mode=inputs.execution_mode,
        graph=inputs.graph,
        resolved_substrate=inputs.resolved_substrate,
        out_of_core_compatible=facts.compatible,
        out_of_core_reject_code=facts.reject_code,
        largest_table_rows=facts.largest_table_rows,
        largest_table_rows_exact=facts.largest_table_rows_exact,
        out_of_core_threshold_rows=inputs.out_of_core_threshold_rows,
        full_frame_reject_rows=inputs.full_frame_reject_rows,
        use_byte_estimate_routing=inputs.use_byte_estimate_routing,
        full_frame_fits_estimate=facts.full_frame_fits_estimate,
        use_probe_routing=inputs.use_probe_routing,
        probe_recovers_full_frame=facts.probe_recovers_full_frame,
    )


def layer2_chunk_decision(inputs: PhysicalPlanInputs) -> tuple[ExecutionPlan | None, bool]:
    """Call `classify_job` (`_planner.py`) directly, mirroring `decide_chunk_
    route`'s own gate. `explain_plan` is dropped from the gate on purpose: it
    only controls whether a classification is computed for EXPLAIN
    surfacing when the route would be `full_frame` regardless (`route_chunked
    = auto_chunk and decision.mode == "chunked"` is False whenever `auto_
    chunk` is False, independent of `explain_plan`), so it carries no
    route-affecting information for DRIVER SELECTION and is not part of
    `PhysicalPlanInputs`.
    """
    from decoy_engine.execution._planner import classify_job

    if not (inputs.auto_chunk and inputs.has_mask_table):
        return None, False
    decision = classify_job(
        dict(inputs.config),
        plan=inputs.plan,
        registry=inputs.registry,
        relationship_graph=inputs.graph,
        substrate=inputs.resolved_substrate,
        source_tables=inputs.caller_sources,
        auto_chunk_threshold_rows=inputs.auto_chunk_threshold_rows,
    )
    route_chunked = inputs.auto_chunk and decision.mode == "chunked"
    return decision, route_chunked


def native_applies(inputs: PhysicalPlanInputs) -> bool:
    """`maybe_run_native_route`'s own top gate (`_native_route.py`), restated
    so `select_driver` can decide whether native was even a candidate
    independent of `inputs.native_admission`'s already-captured verdict."""
    return inputs.native_route_enabled and inputs.has_mask_table


def out_of_core_not_ready_reason(inputs: PhysicalPlanInputs) -> str:
    """Why `out_of_core` was not `out_of_core_ready` in `decide_execution_
    route`'s own terms, for a job that reached `sequential` or `full_frame`
    instead. Mirrors that function's own `out_of_core_ready` conjunction,
    inverted."""
    facts = inputs.out_of_core_facts
    if not facts.compatible:
        return facts.reject_code or "out_of_core_incompatible"
    if facts.largest_table_rows is None:
        return "out_of_core_no_size_signal"
    if facts.largest_table_rows < inputs.out_of_core_threshold_rows:
        return f"out_of_core_below_threshold:{facts.largest_table_rows}"
    return "out_of_core_ready"  # pragma: no cover - contradicts the caller's own route


def _relationship_alternatives(
    inputs: PhysicalPlanInputs, route_reason: str
) -> tuple[RejectedAlternative, ...]:
    """`out_of_core` / `sequential` entries for a job Layer 1 routed to
    `full_frame`. Included ONLY when the job has relationships at all: those
    two modes exist solely for FK jobs (design doc EXECUTION_MODES), so a
    flat non-FK table never had them as plausible alternatives to begin
    with -- listing them would misrepresent an inapplicable lane as a
    declined one.
    """
    if not inputs.has_relationships:
        return ()
    preferred_over_bounded = route_reason in (
        _reasons.ROUTE_BYTE_ESTIMATE_FULL_FRAME_FITS,
        _reasons.ROUTE_PROBE_RECOVERED_FULL_FRAME,
        _reasons.ROUTE_OVERRIDE_FULL_FRAME,
    )
    if preferred_over_bounded:
        # full_frame won on a CONFIRMED fit (or an explicit operator
        # override), not because the bounded routes were ineligible --
        # both alternatives share the same reason token that explains why.
        out_of_core_reason = route_reason
    else:
        out_of_core_reason = out_of_core_not_ready_reason(inputs)
    return (
        RejectedAlternative(DriverId.OUT_OF_CORE, out_of_core_reason, attempted=True),
        RejectedAlternative(DriverId.SEQUENTIAL, route_reason, attempted=True),
    )


def _native_rejected_entry(applies: bool, admission: NativeAdmissionFact) -> RejectedAlternative:
    if not applies:
        return RejectedAlternative(
            DriverId.NATIVE_STREAM, _NATIVE_ROUTE_NOT_APPLICABLE, attempted=False
        )
    return RejectedAlternative(
        DriverId.NATIVE_STREAM, admission.reason or "native_admission_declined", attempted=True
    )


def _chunked_rejected_entry(decision: ExecutionPlan | None) -> RejectedAlternative:
    if decision is None:
        return RejectedAlternative(DriverId.CHUNKED, _CHUNK_NOT_APPLICABLE, attempted=False)
    if decision.mode == "chunked":
        # classify_job itself found the job chunk-admissible; it was not
        # SELECTED here only because native preempted it (the only way this
        # branch is reached with decision.mode == "chunked").
        return RejectedAlternative(
            DriverId.CHUNKED, _reasons.DRIVER_REASON_CHUNKED_ADMITTED, attempted=True
        )
    codes = _reasons.translate_chunked_rejection(
        decision.rejections.get("chunked", decision.reason)
    )
    reason = ";".join(codes) if codes else "chunked_not_admitted"
    return RejectedAlternative(DriverId.CHUNKED, reason, attempted=True)


def select_driver(inputs: PhysicalPlanInputs) -> DriverSelection:
    """The frozen selection precedence (module docstring)."""
    route, route_reason = layer1_route(inputs)

    if route == "sequential":
        rejected = (
            RejectedAlternative(
                DriverId.OUT_OF_CORE, out_of_core_not_ready_reason(inputs), attempted=True
            ),
        )
        return DriverSelection(DriverId.SEQUENTIAL, route_reason, None, rejected)

    if route == "out_of_core":
        return DriverSelection(DriverId.OUT_OF_CORE, route_reason, None, ())

    # route == "full_frame": narrow among native_stream / chunked / full_frame.
    decision, route_chunked = layer2_chunk_decision(inputs)
    applies = native_applies(inputs)
    admission = inputs.native_admission

    if applies and admission.admitted:
        rejected = (
            *_relationship_alternatives(inputs, route_reason),
            _chunked_rejected_entry(decision),
        )
        return DriverSelection(
            DriverId.NATIVE_STREAM, _reasons.DRIVER_REASON_NATIVE_ADMITTED, None, rejected
        )

    if route_chunked:
        if decision is None:  # pragma: no cover - route_chunked implies decision is not None
            raise AssertionError("route_chunked is True but classify_job produced no decision")
        rejected = (
            *_relationship_alternatives(inputs, route_reason),
            _native_rejected_entry(applies, admission),
        )
        return DriverSelection(
            DriverId.CHUNKED, _reasons.DRIVER_REASON_CHUNKED_ADMITTED, decision.reason, rejected
        )

    rejected = (
        *_relationship_alternatives(inputs, route_reason),
        _native_rejected_entry(applies, admission),
        _chunked_rejected_entry(decision),
    )
    return DriverSelection(DriverId.FULL_FRAME, route_reason, None, rejected)


def relationship_role(
    table: str, graph: RelationshipGraph
) -> Literal["independent", "parent", "child"]:
    """A table's role in the FK graph (design doc section 3). A table that is
    both a parent and a child (a chain link) is tagged `"child"`: its
    ordering dependency on an upstream parent is the more consequential fact
    for physical planning than its own downstream children."""
    is_child = any(edge.child_table == table for edge in graph.edges)
    if is_child:
        return "child"
    is_parent = any(edge.parent_table == table for edge in graph.edges)
    if is_parent:
        return "parent"
    return "independent"


def _build_nodes(table: str, inputs: PhysicalPlanInputs) -> tuple[PhysicalNode, ...]:
    """Construct (never select -- design doc section 5) one `PhysicalNode`
    per masking work node on `table`, via the same `requirements_for` /
    `classify_provider` functions the design doc names as the construction
    mechanism."""
    from decoy_engine.execution._runner import build_work_list
    from decoy_engine.execution.native._provider_class import classify_provider
    from decoy_engine.execution.native._requirements import requirements_for

    nodes: list[PhysicalNode] = []
    for work_node in build_work_list(inputs.plan, inputs.registry):
        if work_node.table != table:
            continue
        requirements = requirements_for(work_node, plan=inputs.plan, profile=inputs.profile)
        policy = requirements.fallback_policy
        if policy not in ("native", "python_only"):
            # Invariant 5 (design doc): `reject_large` is a vestigial
            # `FallbackPolicy` value `_fallback_policy()` never emits and
            # must never reach this frozen field. Fail loud, not silently
            # coerced, if that ever changes.
            raise AssertionError(
                f"{table!r}: node {work_node.columns!r} resolved fallback_policy "
                f"{policy!r}, which PhysicalNode must not carry (design doc invariant 5)."
            )
        provider_class = (
            classify_provider(work_node.provider, {}, registry=inputs.registry)
            if work_node.provider
            else None
        )
        node_id = f"{table}:{'+'.join(work_node.columns)}:{work_node.kind}:{work_node.strategy}"
        nodes.append(
            PhysicalNode(
                node_id=node_id,
                table=table,
                columns=work_node.columns,
                kind=work_node.kind,
                strategy=work_node.strategy,
                fallback_policy=policy,
                provider_class=provider_class,
            )
        )
    return tuple(nodes)


def compile_physical_plan(inputs: PhysicalPlanInputs) -> PhysicalPlan:
    """The pure compiler (D2). Executes nothing; raises the same exception a
    live `run_pipeline` call would raise for this snapshot (reject-before-
    read, a forced-mode failure) rather than swallowing or approximating it.
    """
    synthesis = (
        SynthesisStage(
            tables=tuple(
                sorted(name for name, kind in inputs.table_kinds.items() if kind == "generate")
            )
        )
        if inputs.has_generate_table
        else None
    )

    # `layer1_route` (`decide_execution_route`) runs unconditionally in
    # production too (`_pipeline.py` calls `resolve_execution_route` before
    # ever checking `has_mask_table`), so a forced `execution_mode` on a
    # pure-generate job still raises the SAME `ConfigError` here -- the
    # selection is discarded below when there is no mask table to assign it
    # to, but the call must not be skipped, or a forced-mode failure a real
    # `run_pipeline` call would raise goes silently uncaught.
    selection = select_driver(inputs)

    tables: list[PhysicalTable] = []
    if inputs.has_mask_table:
        mask_table_names = sorted(
            name for name, kind in inputs.table_kinds.items() if kind == "mask"
        )
        for table in mask_table_names:
            tables.append(
                PhysicalTable(
                    table=table,
                    driver=selection.driver,
                    driver_reason=selection.reason,
                    driver_reason_detail=selection.reason_detail,
                    rejected_alternatives=selection.rejected_alternatives,
                    relationship_role=relationship_role(table, inputs.graph),
                    substrate=inputs.resolved_substrate,
                    nodes=_build_nodes(table, inputs),
                )
            )

    return PhysicalPlan(
        engine_version=inputs.engine_version,
        plan_hash=inputs.plan_hash(),
        synthesis=synthesis,
        tables=tuple(tables),
    )

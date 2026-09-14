"""D1: `PhysicalPlanInputs` -- the canonical, total, frozen input snapshot the
compiler (`_compiler.compile_physical_plan`, D2) is pure over.

Every route-affecting fact `run_pipeline` consults before dispatch (design
doc docs/plans/2026-09-13-physical-plan-design.md section 3; plan
TASK-4.3-PLAN.md D1) lives here as an already-resolved, already-captured
value. The snapshot holds the REAL objects `run_pipeline` itself builds
(`Plan`, `Profile`, `RelationshipGraph`, `ProviderRegistry`, the caller's
`caller_sources` mapping) rather than re-extracted metadata copies, for one
concrete reason: `decide_execution_route` / `classify_job` / `out_of_core_
admission` are themselves PURE, side-effect-free reads over exactly these
objects (metadata only -- no masking, confirmed by reading each module's
source), so the compiler can call the LIVE production functions directly
instead of reimplementing their branching. That is a deliberate 4.3 design
choice: equivalence for everything those functions decide is definitional
(same function, same arguments), not re-derived and therefore not a source
of drift. What genuinely cannot be captured without a real (I/O-bearing but
non-masking) read -- the native-admission preflight chain, the resolved OOC
memory budget, the byte-estimate/probe verdicts -- is captured ONCE by
`capture_physical_plan_inputs` below and frozen onto the snapshot; the
compiler itself never re-derives or re-reads any of it (D1's "the compiler
is pure over this snapshot").

`capture_physical_plan_inputs` mirrors `run_pipeline`'s own preflight
sequence (`profile_source` -> `compile_plan` -> `build_namespace_registry` /
`build_relationship_graph` -> the routing-signal resolvers -> the native
preflight chain) up to, but never past, the point where `run_pipeline`
itself would dispatch to a driver. It is NOT wired into `run_pipeline` (this
whole package stays disconnected from production, per the Task 4.2 seam's
own sentries) and it executes no masking -- everything it calls is a scan
(schema, row counts, null state, the native lane's bounded preflight pass)
that today's `run_pipeline` already performs before the first byte is
masked.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import pyarrow as pa

from decoy_engine.execution._native_route import peek_and_admit, static_candidacy
from decoy_engine.execution._native_route_preflight import classify_and_preflight
from decoy_engine.profile._readers import LazySource

if TYPE_CHECKING:
    from decoy_engine.execution._transactional_sink import TransactionalSink
    from decoy_engine.plan._types import Plan
    from decoy_engine.profile._types import Profile
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import RelationshipGraph

__all__ = [
    "NativeAdmissionFact",
    "OutOfCoreRoutingFacts",
    "PhysicalPlanInputs",
    "capture_native_admission_fact",
]


# ---------------------------------------------------------------------------
# Native-admission captured fact (D1, round-4 restore; plan LOW-2).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NativeAdmissionFact:
    """The normalized result of the full production native-admission chain
    `static_candidacy` -> `classify_and_preflight` -> `peek_and_admit`
    (`_native_route.py` / `_native_route_preflight.py`), captured up to but
    never past the point `try_native_route` would start masking.

    Normalized from the production `RouteAdmission`
    (`mode`/`admitted`/`reason` -- what actually routes; plan round-4 LOW-2)
    plus `NativeStaticCandidacy` (`table`, `sink_mode`) and, for the utf8
    lane, `NativeBatchAdmission` (`admitted`, `reason`). `PreflightResult.
    column_states` are DELIBERATELY OMITTED: `classify_and_preflight` already
    drops them when building `RouteAdmission` and they do not affect driver
    totality, so re-capturing them here would be redundant diagnostic state,
    not a routing fact.

    `static_candidate=False` means `static_candidacy` declined before any
    source was touched (`static_reason` carries why, including the
    `native_route_disabled_or_no_mask_table` sentinel this module assigns
    when the gate `maybe_run_native_route` itself enforces -- `has_mask_table
    and native_route_enabled` -- was never true, so the live chain was never
    invoked at all). `lane` is set only once `static_candidacy` admits:
    `"utf8_only"` runs the unchanged slice-1 `peek_and_admit` path;
    `"widened"` runs `classify_and_preflight`'s bounded scan. `admitted` is
    the final verdict at whichever level the chain stopped; `reason` is
    `None` only when `admitted` is True.
    """

    table: str | None
    static_candidate: bool
    static_reason: str | None
    sink_mode: Literal["resident", "streaming"] | None
    lane: Literal["utf8_only", "widened"] | None
    admitted: bool
    reason: str | None


_NATIVE_ROUTE_DISABLED_SENTINEL = "native_route_disabled_or_no_mask_table"


def capture_native_admission_fact(
    *,
    has_mask_table: bool,
    native_route_enabled: bool,
    config: Mapping[str, Any],
    execution_mode: str,
    table_kinds: Mapping[str, str],
    caller_sources: Mapping[str, pa.Table | LazySource],
    source_loader: Any,
    sink: TransactionalSink | None,
    fidelity_report: bool,
    graph: RelationshipGraph,
    resolved_substrate: str,
    plan: Plan,
    batch_rows: int,
) -> NativeAdmissionFact:
    """Run the LIVE production preflight chain far enough to know the native
    decision, WITHOUT ever calling `_run_native_streaming` / `run_widened_
    execution` (where masking would start). Mirrors `maybe_run_native_route`'s
    own top gate, then `try_native_route`'s exact branch-conditional order
    (`_native_route_exec.py:521`): `static_candidacy` first; on a candidate,
    `classify_and_preflight` decides `utf8_only` vs. `widened`; `peek_and_
    admit` runs ONLY on the `utf8_only` branch (a widened reject returns
    immediately, a widened admit needs no separate peek -- `classify_and_
    preflight`'s own bounded scan already proved it).
    """
    if not (has_mask_table and native_route_enabled):
        return NativeAdmissionFact(
            table=None,
            static_candidate=False,
            static_reason=_NATIVE_ROUTE_DISABLED_SENTINEL,
            sink_mode=None,
            lane=None,
            admitted=False,
            reason=_NATIVE_ROUTE_DISABLED_SENTINEL,
        )

    candidacy = static_candidacy(
        config=config,
        execution_mode=execution_mode,
        table_kinds=table_kinds,
        caller_sources=caller_sources,
        source_loader=source_loader,
        sink=sink,
        fidelity_report=fidelity_report,
        graph=graph,
        resolved_substrate=resolved_substrate,
    )
    if not candidacy.candidate:
        return NativeAdmissionFact(
            table=None,
            static_candidate=False,
            static_reason=candidacy.reason,
            sink_mode=None,
            lane=None,
            admitted=False,
            reason=candidacy.reason,
        )

    table = candidacy.table
    if table is None:  # pragma: no cover - static_candidacy guarantees this when candidate=True
        raise AssertionError("static_candidacy admitted a candidate with no table name")
    source = caller_sources[table]
    if not isinstance(
        source, LazySource
    ):  # pragma: no cover - static_candidacy already proved this
        raise AssertionError(f"{table!r}: candidacy admitted a non-LazySource entry")

    classification = classify_and_preflight(
        source, table=table, plan=plan, config=config, batch_rows=batch_rows
    )
    if classification.mode == "utf8_only":
        admission = peek_and_admit(source, table=table, plan=plan, batch_rows=batch_rows)
        return NativeAdmissionFact(
            table=table,
            static_candidate=True,
            static_reason=None,
            sink_mode=candidacy.sink_mode,
            lane="utf8_only",
            admitted=admission.admitted,
            reason=admission.reason,
        )
    return NativeAdmissionFact(
        table=table,
        static_candidate=True,
        static_reason=None,
        sink_mode=candidacy.sink_mode,
        lane="widened",
        admitted=classification.admitted,
        reason=classification.reason,
    )


# ---------------------------------------------------------------------------
# Out-of-core routing facts (design doc section 3; plan D1's "Outer-routing
# signal facts" + "Resolved OOC facts" bullets).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutOfCoreRoutingFacts:
    """The `(out_of_core_compatible, reject_code, largest_table_rows,
    largest_table_rows_exact)` signal `_pipeline_routing_signals.
    out_of_core_routing_signals` computes, plus the B1b/B2 byte-estimate and
    probe-recovery verdicts and the resolved OOC memory/disk host budget --
    every value `decide_execution_route` consumes, plus the pre-execution-
    resolvable subset of the OOC-inner-driver's own host budget (design doc
    section 3, section 12 punch-list), that is not itself a pure function of
    `(profile, plan, registry, graph)` alone. Captured once by
    `capture_physical_plan_inputs`; the compiler is pure over these values
    and never re-derives them (`resolve_budget` reads the cgroup/host
    memory ceiling, a real host fact, not a re-derivable pure computation).

    `temp_disk_budget_bytes` / `merge_fan_in` (Task 4.3 remediation H3): the
    OOC-inner driver's own reorder-vs-batch_join route selection
    (`out_of_core._route_policy.decide_route`) additionally consumes these
    two host-budget facts, both resolvable pre-execution exactly like
    `budget_bytes` -- free disk space under the OOC temp root (a `statvfs`
    call, no masking) and the fixed merge-fan-in default. They are captured
    here and hashed so a host with a different disk budget produces a
    different `plan_hash`, but the route_policy per-table DECISION
    (`RouteDecision`/`ReorderCaps`) itself is NOT reproduced: it additionally
    needs the deduplicated parent-key relation row count and the measured
    max sort-payload row width, both read from generated-relation Parquet
    metadata BUILT DURING the OOC run (`_route_policy._parent_key_count`) --
    execution-produced, not pre-execution-observable, so that per-table
    decision is deferred to Task 4.4's execution shadow, exactly like probe-
    recovery. See TASK-4.3-PLAN.md's D1 section for the recorded split.
    """

    compatible: bool
    reject_code: str | None
    largest_table_rows: int | None
    largest_table_rows_exact: bool
    full_frame_fits_estimate: bool | None
    probe_recovers_full_frame: bool | None
    budget_bytes: int | None
    reorder_threshold_rows: int
    temp_disk_budget_bytes: int | None
    merge_fan_in: int


# ---------------------------------------------------------------------------
# PhysicalPlanInputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PhysicalPlanInputs:
    """The total, frozen snapshot `compile_physical_plan` is a pure function
    of (D1). See this module's docstring for why real objects (`Plan`,
    `Profile`, `RelationshipGraph`, `ProviderRegistry`, `caller_sources`) are
    held directly rather than re-extracted into parallel metadata fields.

    `native_companion_reason` (Task 4.3 remediation H3; design doc section 12
    punch-list "native companion probe OUTCOME"): the normalized
    `NativeCompanionStatus.reason` from `native._companion_status.
    native_companion_status()` -- a read-only, never-raises, no-masking probe
    of the optional compiled `decoy-engine-native` companion (present / absent
    / abi-mismatch / kat-corrupt / load-error), captured because it is
    resolvable at decision time exactly like the native-stream admission
    chain. It is NOT yet consumed by any Task 4.3 driver-selection decision --
    the per-node native_chunk-vs-oracle dispatch that DOES key off it
    (`native/_dispatch.py`'s companion-load try/except) is per-node dispatch,
    explicitly deferred to Task 4.4 (this module's docstring; TASK-4.3-
    REMEDIATION.md's 4.3/4.4 boundary line). It is captured and hashed now so
    4.4 can consume it directly from the frozen snapshot without widening the
    shape -- the same "capture now, consume later" precedent D1 already set
    for probe recovery.
    """

    config: Mapping[str, Any]
    plan: Plan
    profile: Profile
    registry: ProviderRegistry
    graph: RelationshipGraph
    table_kinds: Mapping[str, str]
    caller_sources: Mapping[str, pa.Table | LazySource]
    source_loader_present: bool
    resolved_substrate: str
    sink_class_token: str
    execution_mode: str
    fidelity_report: bool
    vault_writer_present: bool
    validators: tuple[Any, ...]
    auto_chunk: bool
    chunk_size_rows: int
    auto_chunk_threshold_rows: int
    out_of_core_threshold_rows: int
    full_frame_reject_rows: int
    use_byte_estimate_routing: bool
    use_probe_routing: bool
    native_route_enabled: bool
    fpe_chunk_count: int
    max_workers: int
    fallback_to_pandas: bool
    out_of_core_facts: OutOfCoreRoutingFacts
    native_admission: NativeAdmissionFact
    native_companion_reason: str
    engine_version: str

    @property
    def has_mask_table(self) -> bool:
        return any(kind == "mask" for kind in self.table_kinds.values())

    @property
    def has_generate_table(self) -> bool:
        return any(kind == "generate" for kind in self.table_kinds.values())

    @property
    def has_relationships(self) -> bool:
        return bool(self.profile.relationships)

    def registry_fingerprint(self) -> str:
        """A stable digest of the registry's work/capability surface (design
        doc section 3's "a work/capability FINGERPRINT from registry"),
        rather than hashing the `ProviderRegistry` object itself (not a
        stable-content-hash contract). `known_providers()` is the registry's
        own public, stable enumeration accessor.

        Hashes the CAPABILITY MATRIX per provider (Task 4.3 remediation H1),
        not just the sorted name list: `_build_nodes` (`_compiler.py`) and
        `classify_provider` (`native/_provider_class.py`) both consult
        `get_capabilities(provider)` -- a `CapabilityMatrix` -- to build
        `PhysicalNode.provider_class`, so two same-name registries whose
        matrices differ (e.g. one `poolable=True`, the other `False`) route
        to different node shapes but, pre-fix, hashed identically. Every
        `CapabilityMatrix` field is a frozen, required Pydantic field
        (`providers_v2/_adapter.py`), so `model_dump()` is a complete,
        stable-keyed serialization of exactly what `_build_nodes` /
        `classify_provider` can observe -- no field selection needed.
        """
        parts = tuple(
            (name, tuple(sorted(self.registry.get_capabilities(name).model_dump().items())))
            for name in sorted(self.registry.known_providers())
        )
        return hashlib.sha256(repr(parts).encode("utf-8")).hexdigest()

    def plan_hash(self) -> str:
        return compute_plan_hash(self)


def _resident_source_fact(name: str, source: pa.Table | LazySource) -> tuple[object, ...]:
    """One source's route-affecting measured content (Task 4.3 remediation
    H1; design doc section 12's "resident-source measured facts"): for a
    resident `pa.Table`, its row count plus ordered `(column, type,
    null_count)` triples -- exactly what `_planner.py`'s runtime-source
    gates read (`_runtime_source_rejections`: row count against the
    auto-chunk threshold, schema + per-column null state against the
    chunk-dtype-stability gate) and what `classify_job`/`layer2_chunk_
    decision` therefore branches on. A `LazySource` carries no resident rows
    to measure, so it contributes a stable marker instead -- its own route-
    affecting content (path, schema) is out of scope for THIS fact family
    (the native-admission chain already captures what it reads from a lazy
    source, in `NativeAdmissionFact`).
    """
    if isinstance(source, LazySource):
        return (name, "lazy_source")
    columns = tuple(
        (field.name, str(field.type), source.column(field.name).null_count)
        for field in source.schema
    )
    return (name, source.num_rows, columns)


def compute_plan_hash(inputs: PhysicalPlanInputs) -> str:
    """`plan_hash` covers exactly the route-affecting members (D1's closing
    line). `Plan.pipeline_config_hash` / `Plan.profile_hash` are themselves
    already canonical content hashes over `config` and the source profile
    respectively, so hashing them transitively covers those two large
    objects without re-serializing them here; every other route-affecting
    field is hashed explicitly. A free function (not a method) so mutmut
    grades it -- decorated-class bodies are skipped by the repo's mutation
    tooling (see `mutmut-decorated-class-limitation` in the dev-rules
    profile / CLAUDE.md's mutation guidance).
    """
    facts = inputs.out_of_core_facts
    admission = inputs.native_admission
    resident_source_facts = tuple(
        _resident_source_fact(name, inputs.caller_sources[name])
        for name in sorted(inputs.caller_sources)
    )
    parts: tuple[object, ...] = (
        inputs.plan.pipeline_config_hash,
        inputs.plan.profile_hash,
        # Resident-source measured facts (H1): `pipeline_config_hash` /
        # `profile_hash` cover the DECLARED config/schema, not the actual
        # resident row/null content `classify_job` reads at capture time --
        # a 2-row vs 4-row resident input on the identical config hashed
        # identically pre-fix, despite routing `full_frame` vs `chunked`.
        # Sorted by table name, so an extra/missing source frame (a changed
        # key set, not just changed content) also changes the hash.
        resident_source_facts,
        inputs.resolved_substrate,
        inputs.sink_class_token,
        inputs.source_loader_present,
        inputs.execution_mode,
        inputs.fidelity_report,
        inputs.vault_writer_present,
        len(inputs.validators),
        inputs.auto_chunk,
        inputs.chunk_size_rows,
        inputs.auto_chunk_threshold_rows,
        inputs.out_of_core_threshold_rows,
        inputs.full_frame_reject_rows,
        inputs.use_byte_estimate_routing,
        inputs.use_probe_routing,
        inputs.native_route_enabled,
        inputs.fpe_chunk_count,
        inputs.max_workers,
        inputs.fallback_to_pandas,
        inputs.registry_fingerprint(),
        tuple(sorted(inputs.table_kinds.items())),
        facts.compatible,
        facts.reject_code,
        facts.largest_table_rows,
        facts.largest_table_rows_exact,
        facts.full_frame_fits_estimate,
        facts.probe_recovers_full_frame,
        facts.budget_bytes,
        facts.reorder_threshold_rows,
        facts.temp_disk_budget_bytes,
        facts.merge_fan_in,
        admission.table,
        admission.static_candidate,
        admission.static_reason,
        admission.sink_mode,
        admission.lane,
        admission.admitted,
        admission.reason,
        inputs.native_companion_reason,
    )
    digest = hashlib.sha256()
    for part in parts:
        digest.update(repr(part).encode("utf-8"))
        digest.update(b"\x1f")
    return digest.hexdigest()

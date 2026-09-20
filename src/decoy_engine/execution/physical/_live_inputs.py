"""Task 4.5 D4: `build_live_physical_plan_inputs` -- the unified slice's own
`PhysicalPlanInputs` constructor, split into its own module rather than
appended to `_inputs.py` to keep that file at its pre-4.5 size (the ~600-LOC
orchestration cap, CLAUDE.md "Engineering best practices").

Distinct from `_snapshot.capture_physical_plan_inputs`, which RE-RUNS
`profile_source` + `compile_plan` from scratch: this constructor builds a
`PhysicalPlanInputs` from `run_pipeline`'s OWN already-produced `profile` /
`plan` / route facts, for the unified slice's non-relationship, non-native
admitted shape only. See `build_live_physical_plan_inputs`'s own docstring
for the full "why" (TASK-4.5-PLAN.md D4).
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.execution.physical._inputs import (
    OutOfCoreRoutingFacts,
    PhysicalPlanInputs,
    deep_freeze_config,
)
from decoy_engine.profile._readers import LazySource

if TYPE_CHECKING:
    from collections.abc import Mapping

    from decoy_engine.plan._types import Plan
    from decoy_engine.profile._types import Profile
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import RelationshipGraph

__all__ = ["build_live_physical_plan_inputs"]

# `_sink_class_token(None)`'s own return value (`_snapshot.py`) -- the D3
# admission predicate guarantees no sink on this lane's admitted path, so
# this constructor never receives one to derive the token from. A named
# constant, not an inline literal, so a bare `"None"` string here does not
# read as a credential to a naive secret-scanner.
_NO_SINK_CLASS_LABEL = "None"


def build_live_physical_plan_inputs(
    *,
    config: Mapping[str, Any],
    plan: Plan,
    profile: Profile,
    registry: ProviderRegistry,
    graph: RelationshipGraph,
    table_kinds: Mapping[str, str],
    caller_sources: Mapping[str, pa.Table | LazySource],
    resolved_substrate: str,
    execution_mode: str,
    fidelity_report: bool,
    vault_writer_present: bool,
    validators: tuple[Any, ...],
    auto_chunk: bool,
    chunk_size_rows: int,
    auto_chunk_threshold_rows: int,
    out_of_core_threshold_rows: int,
    full_frame_reject_rows: int,
    use_byte_estimate_routing: bool,
    use_probe_routing: bool,
    fpe_chunk_count: int,
    max_workers: int,
    fallback_to_pandas: bool,
    out_of_core_reorder_threshold_rows: int | None,
    out_of_core_budget_bytes: int | None,
    engine_version: str,
) -> PhysicalPlanInputs:
    """Task 4.5 D4: build a `PhysicalPlanInputs` from `run_pipeline`'s OWN
    already-produced `profile` / `plan` / route facts, for the unified
    slice's non-relationship, non-native admitted shape ONLY -- never by
    calling `capture_physical_plan_inputs`, which RE-RUNS `profile_source` +
    `compile_plan` and could read a changed file or recompile from evidence
    that has drifted from the live routing decision already made
    (`_pipeline_routing_signals.py` discards those intermediate facts once
    it returns `(route, route_reason)`, so a caller cannot recover them
    without either re-deriving or, as here, reconstructing the values the
    unified slice's own admission predicate already proves are decision-
    inert for this shape).

    One fact family is reconstructed rather than re-computed, because the
    unified-slice admission predicate (`_unified_slice.py`) already proves,
    BEFORE this is ever called, that the job has no relationships:

    - `out_of_core_facts`: `out_of_core_routing_signals` itself short-circuits
      to the inert `(False, None, None, True)` whenever `not (profile.
      relationships and has_mask_table)` -- exactly this job's shape -- so
      calling it costs nothing extra, and it is called here rather than
      hand-duplicated, to track that function's own contract instead of a
      second copy of it. `full_frame_fits_estimate` / `probe_recovers_full_
      frame` are the one exception: `decide_execution_route` only ever READS
      them inside its `byte_estimate_in_scope` branch, which requires
      `has_relationships`, so for a no-relationship job their real value can
      never change the routing outcome. Computing them for real would mean
      re-running the byte estimator (and, on a job that estimate does not
      confirm, spawning the actual measurement probe subprocess) purely to
      produce a number the compiler provably never reads -- exactly the
      wasted re-derivation D4 exists to avoid. Both are set to `None` here:
      the same value `resolve_full_frame_fits_estimate` / `resolve_probe_
      recovery` already return for the (different, but equally out-of-scope)
      "no mask table" shape.
    `resolve_budget` / `resolve_reorder_threshold_rows` / the native
    companion probe are genuine host-config / capability reads, not job-data
    re-derivations (they never touch `caller_sources` or `config`), so they
    are read fresh here exactly as `capture_physical_plan_inputs` does.
    """
    from decoy_engine.execution._pipeline_routing_signals import out_of_core_routing_signals
    from decoy_engine.execution.native._companion_status import native_companion_status
    from decoy_engine.execution.out_of_core import resolve_budget
    from decoy_engine.execution.out_of_core._route_policy import (
        _MERGE_FAN_IN_DEFAULT,
        resolve_reorder_threshold_rows,
    )

    has_mask_table = any(kind == "mask" for kind in table_kinds.values())

    (
        out_of_core_compatible,
        out_of_core_reject_code,
        largest_table_rows,
        largest_table_rows_exact,
    ) = out_of_core_routing_signals(
        profile,
        plan=plan,
        registry=registry,
        graph=graph,
        caller_sources=dict(caller_sources),
        table_kinds=dict(table_kinds),
        has_mask_table=has_mask_table,
    )
    resolved_budget = resolve_budget(out_of_core_budget_bytes)
    out_of_core_facts = OutOfCoreRoutingFacts(
        compatible=out_of_core_compatible,
        reject_code=out_of_core_reject_code,
        largest_table_rows=largest_table_rows,
        largest_table_rows_exact=largest_table_rows_exact,
        full_frame_fits_estimate=None,
        probe_recovers_full_frame=None,
        budget_bytes=resolved_budget.budget_bytes,
        reorder_threshold_rows=resolve_reorder_threshold_rows(out_of_core_reorder_threshold_rows),
        merge_fan_in=_MERGE_FAN_IN_DEFAULT,
    )

    return PhysicalPlanInputs(
        config=deep_freeze_config(config),
        plan=plan,
        profile=profile,
        registry=registry,
        graph=graph,
        table_kinds=MappingProxyType(dict(table_kinds)),
        caller_sources=caller_sources,
        source_loader_present=False,
        resolved_substrate=resolved_substrate,
        sink_class_token=_NO_SINK_CLASS_LABEL,
        execution_mode=execution_mode,
        fidelity_report=fidelity_report,
        vault_writer_present=vault_writer_present,
        validators=validators,
        auto_chunk=auto_chunk,
        chunk_size_rows=chunk_size_rows,
        auto_chunk_threshold_rows=auto_chunk_threshold_rows,
        out_of_core_threshold_rows=out_of_core_threshold_rows,
        full_frame_reject_rows=full_frame_reject_rows,
        use_byte_estimate_routing=use_byte_estimate_routing,
        use_probe_routing=use_probe_routing,
        fpe_chunk_count=fpe_chunk_count,
        max_workers=max_workers,
        fallback_to_pandas=fallback_to_pandas,
        out_of_core_facts=out_of_core_facts,
        native_companion_reason=native_companion_status().reason,
        engine_version=engine_version,
    )

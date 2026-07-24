"""`estimate_job_capacity`: the estimate-only entrypoint for the out-of-core-FK
memory capacity gate (v1 CLI capacity checker, `decoy preflight` / `decoy run`).

R4 (Codex plan-review, docs/plans/2026-07-24-oom-checker-cli-v1.md): this is an
"OOC-FK engine-gate capacity checker", NOT a whole-job OOM guarantee. The CLI
fully loads every source into memory BEFORE it calls the engine at all, so an
ingestion `MemoryError` or OS OOM-kill happens before this gate ever runs, and
the resident-floor estimate below excludes that ingestion peak entirely. What
this DOES check honestly: would `run_pipeline`'s out-of-core-FK route (the one
route with a resident-memory floor that has no runtime spill backstop) refuse
this job, using the SAME evaluator (`evaluate_capacity`) the mid-run gate uses.

R1 anti-drift: this module derives its inputs by calling the SAME engine
primitives `run_pipeline` calls, in the same order, up through the routing
decision -- `profile_source`, `compile_plan`, `build_relationship_graph`,
`decide_execution_route` -- rather than re-deriving the FK graph or the
row-count/fan-in signals from scratch. It stops BEFORE dispatching to a
runner: no mask/generate work ever executes here.

Two deliberate, documented simplifications versus a real `decoy run`:

1. `use_byte_estimate_routing=False` / `use_probe_routing=False`. Real runs
   default both True, which can recover a large FK job to `full_frame` when a
   byte-level estimate (or a measured micro-probe) confirms it fits -- but
   that recovery only prices RESIDENT sample data, and this estimator never
   materializes a source to get one (R6: no full-frame read in the estimate
   path). Skipping it means this checker is occasionally MORE conservative
   than the real run (it may flag a job as out-of-core-bound that the real
   run's byte-estimate would actually admit to `full_frame`), never less --
   the safe direction for a capacity gate, and consistent with R4's framing
   that this checks the out-of-core-FK route specifically, not whole-job
   fit.
2. Row counts for the capacity floor itself (not the routing size signal)
   trust ONLY an exact per-table count (`TableProfile.row_count_exact`):
   Parquet/fixed_width footer counts, never a CSV byte-size estimate. A
   parent table priced only by estimate makes the whole verdict `UNKNOWN`
   (R6) -- this checker refuses to gate a memory hard-fail on a guess.

Source-path resolution matches `decoy run`'s own convention (R2): relative
`sources[*].path` entries resolve against the caller-supplied `base_dir` (the
pipeline YAML's directory, exactly like `run.py`'s `_resolve_path`), not the
process's current working directory -- so a `decoy preflight` run from a
different directory than `decoy run` still evaluates the identical files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._pipeline import classify_table_kinds
from decoy_engine.execution._pipeline_route_exec import (
    _incoming_edge_counts,
    _max_concurrent_ooc_instances,
)
from decoy_engine.execution._pipeline_routing import decide_execution_route
from decoy_engine.execution._pipeline_routing_signals import (
    largest_mask_table_rows_from_profile,
    out_of_core_admission,
)
from decoy_engine.execution.out_of_core import resolve_ooc_memory_limit
from decoy_engine.execution.out_of_core._memory_estimate import (
    CapacityEstimate,
    CapacityInputs,
    CapacityVerdict,
    evaluate_capacity,
)

__all__ = ["estimate_job_capacity"]

_OUT_OF_CORE_ROUTE = "out_of_core"

# The mid-run gate raises `out_of_core_parent_rows_unresolved`
# (`_pipeline_route_exec._parent_table_row_counts`) when a parent table has no
# resolvable source at all -- a real config/graph inconsistency, not a
# priceability question. Reused verbatim here (same code, same fail-closed
# posture) rather than inventing a second code for the identical defect.
_PARENT_ROWS_UNRESOLVED_CODE = "out_of_core_parent_rows_unresolved"


def _resolve_source_path(raw_path: str, base_dir: Path) -> Path:
    """Mirror the CLI's `run.py:_resolve_path`: a relative path resolves
    against `base_dir` (the pipeline YAML's directory); an absolute path
    passes through unchanged. Kept as a private mirror rather than an
    import across repos -- the CLI and engine are separate packages."""
    p = Path(raw_path)
    return p if p.is_absolute() else (base_dir / p).resolve()


def _resolved_config_copy(config_dump: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    """A shallow copy of `config_dump` with every file-type source's `path`
    resolved to an absolute string against `base_dir`.

    Needed because `profile_source`'s file readers (`_build_file_source`)
    open `descriptor["path"]` verbatim, with no base-directory concept of
    their own -- `decoy run` only gets this right today because it loads
    every source itself (via its own `_resolve_path`) before calling the
    engine at all. This estimator has no such eager load to lean on, so it
    resolves the config's OWN path strings up front instead, guaranteeing
    the exact same file is read regardless of the calling process's CWD
    (R2: preflight and run must resolve sources identically).
    """
    sources = config_dump.get("sources")
    if not isinstance(sources, dict) or not sources:
        return config_dump
    resolved_sources: dict[str, Any] = {}
    for name, descriptor in sources.items():
        if (
            isinstance(descriptor, dict)
            and descriptor.get("type") == "file"
            and isinstance(descriptor.get("path"), str)
        ):
            descriptor = dict(descriptor)
            descriptor["path"] = str(_resolve_source_path(descriptor["path"], base_dir))
        resolved_sources[name] = descriptor
    resolved = dict(config_dump)
    resolved["sources"] = resolved_sources
    return resolved


def _not_applicable(route: str, message: str) -> CapacityEstimate:
    return CapacityEstimate(
        verdict=CapacityVerdict.NOT_APPLICABLE,
        code=None,
        needed_bytes=None,
        available_bytes=None,
        route=route,
        message=message,
    )


def estimate_job_capacity(
    config_dump: dict[str, Any],
    base_dir: Path | str,
    *,
    budget_bytes: int | None = None,
) -> CapacityEstimate:
    """Estimate whether `config_dump` would clear the out-of-core-FK route's
    memory gate, WITHOUT running the pipeline.

    `config_dump` MUST be `PipelineConfig.model_validate(raw).model_dump()`
    (the same normalized shape `run_pipeline` takes) -- no re-validation
    here. `base_dir` is the directory relative `sources[*].path` entries
    resolve against (the pipeline YAML's own directory, matching `decoy
    run`). `budget_bytes` is an explicit override of the memory ceiling this
    job would be sized against; `None` (the common case) lets
    `resolve_ooc_memory_limit` detect it via `detect_effective_memory_bytes`
    -- the SAME call, with the SAME `max_concurrent_instances` derivation,
    the real out-of-core route makes at its own one call site, so a job with
    no explicit override resolves to byte-for-byte the same budget in both
    `decoy preflight` and `decoy run`.

    Returns a `CapacityEstimate` whose `verdict` is `NOT_APPLICABLE` when this
    job would not take the out-of-core route at all (no relationships, an
    ineligible/incompatible shape, a job the engine would reject before read,
    or a job the routing decision sends to `full_frame`/`sequential`
    instead), `UNKNOWN` when the row count or the memory ceiling cannot be
    priced exactly, `FIT`/`INSUFFICIENT` otherwise -- see `evaluate_capacity`
    for the shared decision this defers to once the inputs are derived.

    Propagates any OTHER exception (a genuine config/compile defect,
    `PlanCompileError`, an unreadable source, ...) rather than swallowing it
    into a verdict (R3): an unexpected failure here is a real problem the
    caller needs to see, not a silent "capacity unknown".
    """
    from decoy_engine import __version__ as engine_version
    from decoy_engine.plan import compile_plan
    from decoy_engine.plan._seed import _normalize_job_seed_int
    from decoy_engine.profile import profile_source
    from decoy_engine.providers_v2 import get_default_registry
    from decoy_engine.relationships import (
        build_namespace_registry,
        build_relationship_graph,
        check_orphan_fk_policy_completeness,
    )

    base_dir_path = Path(base_dir)
    config = _resolved_config_copy(config_dump, base_dir_path)

    table_kinds = classify_table_kinds(config)
    has_mask_table = any(kind == "mask" for kind in table_kinds.values())
    has_generate_table = any(kind == "generate" for kind in table_kinds.values())

    job_seed = _normalize_job_seed_int(config)
    # Bounded residency (the default): cheap row-count metadata plus a
    # <=10k-row sample per source, never a full-frame read -- the SAME call
    # `run_pipeline` makes before it decides a route (`_pipeline.py`), reused
    # here rather than re-derived so the relationship graph and per-table
    # row counts this function prices are the identical numbers the real run
    # would compute for the same config.
    profile = profile_source(config, seed=job_seed)

    if not profile.relationships:
        return _not_applicable(
            "none",
            "this job declares no relationships; the out-of-core-FK capacity check "
            "only applies to jobs with FK relationships.",
        )

    registry = get_default_registry()
    plan = compile_plan(config, profile, decoy_engine_version=engine_version)
    ns_registry = build_namespace_registry(config, profile)
    orphan_lookup = check_orphan_fk_policy_completeness(config, profile.relationships)
    graph = build_relationship_graph(
        profile.relationships, namespace_registry=ns_registry, orphan_policy_lookup=orphan_lookup
    )

    out_of_core_compatible, out_of_core_reject_code = out_of_core_admission(
        plan, registry=registry, graph=graph
    )
    size_signal = largest_mask_table_rows_from_profile(profile, table_kinds=table_kinds)
    largest_table_rows, largest_table_rows_exact = (
        size_signal if size_signal is not None else (None, True)
    )

    try:
        route, _route_reason = decide_execution_route(
            profile,
            has_generate_table=has_generate_table,
            has_mask_table=has_mask_table,
            validators=(config.get("validators") or []),
            fidelity_report=False,
            vault_writer=None,
            execution_mode="auto",
            graph=graph,
            resolved_substrate="pandas",
            out_of_core_compatible=out_of_core_compatible,
            out_of_core_reject_code=out_of_core_reject_code,
            largest_table_rows=largest_table_rows,
            largest_table_rows_exact=largest_table_rows_exact,
            # See module docstring point 1: real runs default both True; this
            # estimator turns them off because their "recovery to full_frame"
            # path needs resident sample data this function never reads.
            use_byte_estimate_routing=False,
            use_probe_routing=False,
        )
    except ExecutionError as exc:
        return _not_applicable(
            "rejected_before_read",
            f"this job would be rejected before read by the engine's routing guard "
            f"({exc.code}): {exc.message} The out-of-core-FK capacity check only "
            "applies to a job that actually reaches that route.",
        )

    if route != _OUT_OF_CORE_ROUTE:
        return _not_applicable(
            route,
            f"this job's execution route is {route!r}, not out-of-core-FK; v1's "
            "capacity check only covers the out-of-core-FK route.",
        )

    parent_tables = {edge.parent_table for edge in graph.edges}
    profile_by_name = {t.name: t for t in profile.tables}
    parent_table_rows: dict[str, int] = {}
    unresolved: set[str] = set()
    for table in parent_tables:
        table_profile = profile_by_name.get(table)
        if table_profile is None:
            raise ExecutionError(
                code=_PARENT_ROWS_UNRESOLVED_CODE,
                message=(
                    f"out-of-core capacity estimate cannot price table {table!r}: it has an "
                    "outgoing FK edge but no declared source in config['sources']."
                ),
            )
        if not table_profile.row_count_exact:
            # R6: a CSV byte-size estimate is never trusted for the capacity
            # floor itself (only for the coarser routing size signal above).
            unresolved.add(table)
            continue
        parent_table_rows[table] = table_profile.row_count

    incoming_edge_counts = _incoming_edge_counts(graph)
    # `decoy run` never passes a sink to `run_pipeline` (run.py has no sink
    # flag), so `sink=False` here is not a simplification -- it is the exact
    # value the real run always resolves.
    max_concurrent = _max_concurrent_ooc_instances(graph, sink=False)
    # Mirrors `run_out_of_core_route`'s own fail-open try/except verbatim:
    # `resolve_ooc_memory_limit(None, ...)` calls `detect_effective_memory_
    # bytes()` internally, which raises a coded `ExecutionError` when NEITHER
    # a cgroup limit nor host RAM is readable. The real run path treats that
    # as "no budget to gate against" rather than a hard failure; this
    # estimator must fail open the SAME way (R3: undetectable RAM is
    # EXPECTED indeterminacy, verdict UNKNOWN, not a propagated crash). An
    # explicit caller-supplied `budget_bytes` still raises -- a caller who
    # passed a bad explicit value gets told, exactly like the run path.
    resolved_budget_bytes: int | None
    try:
        resolved_budget_bytes = resolve_ooc_memory_limit(
            budget_bytes, max_concurrent_instances=max_concurrent
        ).budget_bytes
    except ExecutionError:
        if budget_bytes is not None:
            raise
        resolved_budget_bytes = None

    inputs = CapacityInputs(
        route=_OUT_OF_CORE_ROUTE,
        parent_table_rows=parent_table_rows,
        incoming_edge_counts=incoming_edge_counts,
        sink=False,
        unresolved_parent_tables=frozenset(unresolved),
    )
    return evaluate_capacity(inputs, resolved_budget_bytes)

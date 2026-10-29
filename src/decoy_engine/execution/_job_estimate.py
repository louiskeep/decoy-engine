"""Advisory job estimate: route + peak memory + runtime band (job-time-estimator Slice 2).

`estimate_job` is the shared, PUBLIC primitive the CLI (`decoy preflight`) and the
platform (preview / job create) call to show "this will take ~X and need ~Y RAM"
BEFORE running. Advisory per the §CAP lock: it never gates, never raises (an
unpriceable or error case returns a flagged result, not an exception), and never
changes routing.

It composes three existing pieces plus the new runtime band:
  route   -- `decide_execution_route` (the same row-count decision the real run
             makes; the byte-estimate refinement is a run-time concern we skip here)
  peak    -- `estimate_peak_bytes` over the job's size specs
  runtime -- `_throughput_calibration` (coarse, host-caveated, confidence-banded)

Input is a BOUNDED SAMPLE, not metadata-only and not a full read: `profile_source`
reads up to ~10k rows per source. The reported route is a coarse layer-1 prediction
(it omits later chunk/native dispatch and mixed admission).

Pre-run string pricing: like the engine's own byte-estimate router, a variable-
width column with no resident sample is UNPRICEABLE (the profile carries no
string-length statistic). Such a job returns `priceable=False` with the route
still reported, honestly, rather than a guessed number.

Route decision is MIRRORED from `capacity.py`'s prelude rather than shared: that
module early-returns for a non-relational job BEFORE compiling, so a shared helper
would change its tested behavior. The route itself comes from `decide_execution_route`
(the engine's own tested function, the same call `run_pipeline` makes), so its
correctness is inherited; we deliberately take the row-count route and skip
`capacity.py`'s out-of-core byte-estimate PROMOTION (a run-time refinement), so the
two functions' returned routes can differ for an OOC-eligible FK job -- by design.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from decoy_engine.execution._throughput_calibration import (
    RuntimeBand,
    estimate_generation_runtime_band,
    estimate_mask_runtime_band,
)
from decoy_engine.fileaccess import FileAccess, entry_point

_PEAK_PATHS = frozenset({"full_frame", "out_of_core", "sequential"})


@dataclass(frozen=True)
class JobEstimate:
    """Advisory estimate for a job. `route` is always reported; `peak_bytes` and
    `runtime` are None when the job is unpriceable (a variable-width column with
    no sample, or an error deriving the estimate), with `notes` explaining why."""

    route: str
    priceable: bool
    peak_bytes: int | None = None
    runtime: RuntimeBand | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


def _generation_rows(config: dict[str, Any]) -> tuple[int, bool]:
    """Total declared generate-table row count, and whether any count was declared.

    An explicit row_count of 0 is declared zero-work, distinct from an undeclared
    count: the caller prices the former and flags the latter unpriceable."""
    total = 0
    declared = False
    for table in config.get("tables", []) or []:
        rows = table.get("row_count")
        if rows is None:
            rows = table.get("rows")
        if isinstance(rows, int) and not isinstance(rows, bool) and rows >= 0:
            total += rows
            declared = True
    return total, declared


@entry_point
def estimate_job(
    config_dump: dict[str, Any], base_dir: Path | str, *, file_access: FileAccess | None = None
) -> JobEstimate:
    """Estimate `config_dump`'s route, peak memory, and runtime band without
    running it. Advisory: returns a flagged `JobEstimate`, never raises.

    `config_dump` is `PipelineConfig.model_validate(raw).model_dump()`; `base_dir`
    is the directory relative `sources[*].path` resolve against (as `decoy run`)."""
    try:
        return _estimate(config_dump, base_dir)
    except Exception as exc:
        # Name only the exception TYPE: formatting `exc` itself can raise (a broken
        # __str__), which would escape the advisory contract's never-raises guarantee.
        return JobEstimate(
            route="unknown",
            priceable=False,
            notes=(f"estimate unavailable ({type(exc).__name__})",),
        )


def _estimate(config_dump: dict[str, Any], base_dir: Path | str) -> JobEstimate:
    from decoy_engine import __version__ as engine_version
    from decoy_engine.execution._mem_estimate import estimate_peak_bytes, raw_data_bytes
    from decoy_engine.execution._pipeline_context import classify_table_kinds
    from decoy_engine.execution._pipeline_routing import decide_execution_route
    from decoy_engine.execution._pipeline_routing_signals import (
        _mask_specs,
        largest_mask_table_rows_from_profile,
        out_of_core_admission,
    )
    from decoy_engine.execution.capacity import _resolved_config_copy
    from decoy_engine.plan import compile_plan
    from decoy_engine.plan._seed import _normalize_job_seed_int
    from decoy_engine.profile import profile_source
    from decoy_engine.providers_v2 import get_default_registry
    from decoy_engine.relationships import (
        build_namespace_registry,
        build_relationship_graph,
        check_orphan_fk_policy_completeness,
    )

    config = _resolved_config_copy(config_dump, Path(base_dir))
    table_kinds = classify_table_kinds(config)
    has_mask = any(k == "mask" for k in table_kinds.values())
    has_generate = any(k == "generate" for k in table_kinds.values())

    profile = profile_source(config, seed=_normalize_job_seed_int(config))
    registry = get_default_registry()
    plan = compile_plan(config, profile, decoy_engine_version=engine_version)
    ns_registry = build_namespace_registry(config, profile)
    orphan_lookup = check_orphan_fk_policy_completeness(config, profile.relationships)
    graph = build_relationship_graph(
        profile.relationships, namespace_registry=ns_registry, orphan_policy_lookup=orphan_lookup
    )
    compatible, reject_code = out_of_core_admission(plan, registry=registry, graph=graph)
    size_signal = largest_mask_table_rows_from_profile(profile, table_kinds=table_kinds)
    largest_rows, largest_rows_exact = size_signal if size_signal is not None else (None, True)

    route, _reason = decide_execution_route(
        profile,
        has_generate_table=has_generate,
        has_mask_table=has_mask,
        validators=(config.get("validators") or []),
        fidelity_report=False,
        vault_writer=None,
        execution_mode="auto",
        graph=graph,
        resolved_substrate="pandas",
        out_of_core_compatible=compatible,
        out_of_core_reject_code=reject_code,
        largest_table_rows=largest_rows,
        largest_table_rows_exact=largest_rows_exact,
        # Row-count route (no resident read); the byte-estimate refinement is a
        # run-time concern. Advisory estimate, so this is the honest coarse route.
        use_byte_estimate_routing=False,
        use_probe_routing=False,
        # STORM needs the resident output, so it disqualifies sequential/out-of-core.
        run_storm=bool(config.get("run_storm")),
    )

    # A mixed job's generated volume is not priced; refuse rather than under-price.
    if has_generate and has_mask:
        return JobEstimate(
            route,
            priceable=False,
            notes=("mixed mask+generate job: generated volume is not priced",),
        )

    # Generate-only: runtime is row/provider-bound; peak from generation is not
    # priced here (v1 scopes byte-pricing to mask tables), so priceable=True with
    # peak_bytes=None is intentional.
    if has_generate and not has_mask:
        rows, declared = _generation_rows(config)
        if not declared:
            return JobEstimate(route, priceable=False, notes=("generate row count undeclared",))
        return JobEstimate(route, priceable=True, runtime=estimate_generation_runtime_band(rows))

    mask_tables = [t for t in profile.tables if table_kinds.get(t.name) == "mask"]
    # No resident sources here (pre-run), so a variable-width column with no
    # string-length statistic is unpriceable -- the same honest limit the
    # engine's own byte-estimate router has.
    specs = _mask_specs(mask_tables, {})
    raw = raw_data_bytes(specs)
    if not raw.is_priceable:
        cols = ", ".join(f"{table}.{column}" for table, column in raw.unpriceable_columns[:3])
        return JobEstimate(
            route,
            priceable=False,
            notes=(f"unpriceable variable-width column(s) without a sample: {cols}",),
        )

    path = route if route in _PEAK_PATHS else "full_frame"
    peak = estimate_peak_bytes(specs, path)  # type: ignore[arg-type]
    runtime = estimate_mask_runtime_band(raw.priceable_bytes, route)
    notes: tuple[str, ...] = ()
    if route not in _PEAK_PATHS:
        notes = (f"route {route!r} priced as full_frame (no dedicated model)",)
    return JobEstimate(
        route,
        priceable=peak.estimated_bytes is not None,
        peak_bytes=peak.estimated_bytes,
        runtime=runtime,
        notes=notes,
    )


__all__ = ["JobEstimate", "estimate_job"]

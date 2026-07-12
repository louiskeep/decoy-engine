"""Size + out-of-core-admission SIGNALS that feed `run_pipeline`'s route
DECISIONS, split out of `_pipeline_routing` to hold the 600-LOC orchestration
cap (CLAUDE.md section "Engineering best practices").

This module only COMPUTES the signals `decide_execution_route` consumes -- the
static out-of-core compatibility verdict and the largest-mask-table size (with
its exact-vs-estimated flag). It makes no routing decision itself; the fixed
decision order lives in `_pipeline_routing`, which re-exports these so the
`run_pipeline` call sites keep a single `_pipeline_routing.<name>` surface.

The size signal is where the SC7a bounded-profiling work pays off: the
per-table `row_count` the (now cheap) profile carries lets the SC2 size gates
fire on the LAZY (`sources={}`, `source_loader`) path -- closing the
consultant-2026-07-09 F2 hole where `largest_mask_table_rows` returned None off
resident Arrow sources and the reject-before-read was silently disabled for
exactly the bounded input shape that most needs it.
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.profile._readers import LazySource

if TYPE_CHECKING:
    from decoy_engine.plan._types import Plan
    from decoy_engine.profile._types import Profile, TableProfile
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import RelationshipGraph

__all__ = [
    "byte_estimate_full_frame_fits",
    "largest_mask_table_rows",
    "largest_mask_table_rows_from_profile",
    "out_of_core_admission",
    "out_of_core_routing_signals",
    "resolve_execution_route",
    "resolve_full_frame_fits_estimate",
    "resolve_probe_recovery",
]


def out_of_core_admission(
    plan: Plan,
    *,
    registry: ProviderRegistry,
    graph: RelationshipGraph,
) -> tuple[bool, str | None]:
    """Static out-of-core compatibility check: `(compatible, primary_code)`.

    Delegates to `check_out_of_core_compatibility` (the same fail-closed gate
    SC1/SC2-part-1 hardened and the parity harness pins) so routing and the
    runner's own pre-flight guard cannot disagree on the admitted surface. Pure
    and cheap: a static read of the compiled plan + relationship graph (no
    per-row work), safe to call on the dispatch path. `primary_code` names the
    first rejection so the reject-before-read message and the forced-mode error
    can explain WHY the route declined.
    """
    from decoy_engine.execution._runner import build_work_list, order_work
    from decoy_engine.execution.out_of_core._compat import check_out_of_core_compatibility

    work = order_work(build_work_list(plan, registry), graph)
    compat = check_out_of_core_compatibility(plan, work, graph)
    return compat.accepted, compat.primary_code


def largest_mask_table_rows(
    caller_sources: dict[str, pa.Table | LazySource],
    *,
    table_kinds: dict[str, str],
) -> int | None:
    """Row count of the largest RESIDENT mask-kind source, or None if unknown.

    The out-of-core / reject size gate keys off the largest mask table because
    the full-frame FK memory model (docs/relationships-memory-scaling.md §6) is
    linear in rows-per-table and dominated by the widest/tallest resident
    frame. Returns None when no mask source is resident (e.g. a lazy
    `source_loader` path with an empty `sources` dict): the profile-metadata
    count (`largest_mask_table_rows_from_profile`) then supplies the size signal
    instead of leaving the gate blind. `num_rows` is Arrow array metadata, so
    this is O(tables), not O(rows).
    """
    mask_rows = [
        src.num_rows for name, src in caller_sources.items() if table_kinds.get(name) == "mask"
    ]
    return max(mask_rows) if mask_rows else None


def largest_mask_table_rows_from_profile(
    profile: Profile,
    *,
    table_kinds: dict[str, str],
) -> tuple[int, bool] | None:
    """`(rows, exact)` of the largest mask-kind table by the PROFILE row count,
    or None when the profile carries no mask-kind table.

    The SC7a bounded-profiling core made `TableProfile.row_count` a cheap
    metadata count (exact from a Parquet/fixed_width footer, an
    `row_count_exact=False` byte-size estimate for CSV) that no longer requires
    reading the whole source. This function surfaces that count as the size
    signal the SC2 gates need, so the reject-before-read fires on the LAZY
    (`sources={}`, `source_loader`) path where `largest_mask_table_rows` (which
    reads resident Arrow metadata) returns None -- the `None` hole the F2
    finding flagged. `exact` rides the largest table's `row_count_exact` so the
    gate can distinguish a trusted footer count from a CSV estimate (see
    `decide_execution_route`). O(tables): a pass over already-computed profile
    metadata, no per-row work.
    """
    mask_tables = [t for t in profile.tables if table_kinds.get(t.name) == "mask"]
    if not mask_tables:
        return None
    largest = max(mask_tables, key=lambda t: t.row_count)
    return largest.row_count, largest.row_count_exact


def _resolve_largest_mask_table_rows(
    profile: Profile,
    *,
    caller_sources: dict[str, pa.Table | LazySource],
    table_kinds: dict[str, str],
) -> tuple[int | None, bool]:
    """Reconcile the resident and profile-metadata size signals into the
    `(rows, exact)` the SC2 gates key off, RECONCILING PER MASK TABLE (H1).

    The reconciliation is per-table, keyed by table identity -- NOT a single
    scalar comparison of the two aggregate maxes. That distinction is the H1
    fix: partial residency is a first-class supported shape (`run_out_of_core_route`
    resolves *missing* tables through `source_loader`), so a job can pass a tiny
    resident parent while relying on a lazy loader for a huge child. A scalar
    "any resident source exists -> trust the resident max" rule threw the huge
    lazy table's exact profile count away and let it hide behind the tiny
    resident one, re-opening the F2 full-frame OOM hole. Instead, for EACH mask
    table we pick the count that table actually has:

    - RESIDENT table (present in `caller_sources`): its Arrow `num_rows` is the
      TRUE count of the data that will be masked (the runner consumes that
      resident table), so it is that table's routing count and is always exact.
      The profile count is kept as a per-table cross-check: when it is EXACT
      (Parquet/fixed_width metadata) it should match the resident count, and a
      mismatch is surfaced with a warning rather than a hard raise. A raise would
      be wrong because a caller may legitimately pass a resident source that
      differs from the on-disk descriptor the profile read (e.g.
      pre-filtered/transformed input, which run_pipeline masks as given, and
      which profile_source never sees -- it reads config-path descriptors only);
      the resident count is authoritative for such a run. A CSV *estimate* is
      expected to differ from a resident exact count, so it is reconciled
      silently.
    - LAZY table (absent from `caller_sources`): only the profile's cheap
      `row_count` exists, so it is that table's routing count, carrying its
      `row_count_exact` flag (False for a CSV byte-estimate). This closes the
      pre-SC7b `largest_mask_table_rows() is None` hole.

    The final signal is the MAX across mask tables of each table's resolved
    count, threading through the `row_count_exact` of whichever table produced
    that max (the downstream estimated-CSV-reject branch keys off it). So a
    fully-resident job routes on resident counts (unchanged from pre-SC7b), a
    fully-lazy job routes on profile counts, and a MIXED job routes on whichever
    count each table individually has -- a huge lazy table can no longer hide
    behind a small resident one.

    `profile.tables` is the authoritative full set of mask tables: profile_source
    reads every config-declared source descriptor (it never sees resident
    `sources`), so iterating profile mask tables covers every table an FK+mask
    job can route on.
    """
    mask_tables = [t for t in profile.tables if table_kinds.get(t.name) == "mask"]
    if not mask_tables:
        # No mask table in the profile: defensively fall back to any resident
        # mask signal. Off the FK+mask shape this resolver is not consulted, so
        # this branch is belt-and-suspenders rather than a live routing path.
        return largest_mask_table_rows(caller_sources, table_kinds=table_kinds), True

    best_rows: int | None = None
    best_exact = True
    for table in mask_tables:
        resident = caller_sources.get(table.name)
        if resident is not None:
            table_rows = resident.num_rows
            table_exact = True
            if table.row_count_exact and table_rows != table.row_count:
                warnings.warn(
                    f"Mask-table row count disagrees for table {table.name!r} "
                    f"between the resident source ({table_rows:,}) and the exact "
                    f"profile metadata ({table.row_count:,}); routing on the "
                    f"resident count. This is expected only when the caller "
                    f"passes a resident source that differs from the on-disk "
                    f"descriptor; otherwise it may indicate a profile-reader bug.",
                    RuntimeWarning,
                    stacklevel=2,
                )
        else:
            table_rows = table.row_count
            table_exact = table.row_count_exact
        if best_rows is None or table_rows > best_rows:
            best_rows, best_exact = table_rows, table_exact
    return best_rows, best_exact


def out_of_core_routing_signals(
    profile: Any,
    *,
    plan: Plan,
    registry: ProviderRegistry,
    graph: RelationshipGraph,
    caller_sources: dict[str, pa.Table | LazySource],
    table_kinds: dict[str, str],
    has_mask_table: bool,
) -> tuple[bool, str | None, int | None, bool]:
    """The `(out_of_core_compatible, reject_code, largest_table_rows,
    largest_table_rows_exact)` tuple `decide_execution_route`'s SC2 gates
    consume.

    Computed only for relationship jobs with a mask table -- a static plan +
    profile-metadata read, so non-FK jobs pay nothing and keep the pre-SC2
    routing. Off that shape the tuple is the inert `(False, None, None, True)`
    default, which makes every SC2 gate a no-op.

    The size signal comes from the profile's cheap `row_count` (SC7a) on the
    lazy path and from resident Arrow metadata on the resident path (see
    `_resolve_largest_mask_table_rows`); `largest_table_rows_exact` flags a CSV
    byte-size estimate so the reject gate can raise a distinct, actionable code
    instead of trusting an estimate at a hard threshold.
    """
    if not (getattr(profile, "relationships", None) and has_mask_table):
        return False, None, None, True
    compatible, reject_code = out_of_core_admission(plan, registry=registry, graph=graph)
    rows, exact = _resolve_largest_mask_table_rows(
        profile, caller_sources=caller_sources, table_kinds=table_kinds
    )
    return compatible, reject_code, rows, exact


def _resident_column_arrays(
    resident: pa.Table | LazySource | None, profile_table: TableProfile
) -> dict[str, pa.Array | pa.ChunkedArray]:
    """`{column_name: array}` for `profile_table`'s columns present in a
    RESIDENT source, or `{}` for a lazy (not-yet-loaded) table.

    Zero-copy: `pa.Table.column` returns a view into the existing Arrow
    buffer, not a copy, so building this mapping costs nothing per row.

    TB-1: a `LazySource` (`_isolated_worker._load_sources`) is treated
    exactly like `None` here -- it has no resident column buffers to
    sample without a full read, and this signal (the byte-estimate /
    probe-recovery admission path, both flag-gated default-OFF) must never
    force one just to sample. `None`/unsampleable is already the documented
    safe direction (see this function's callers): an unpriceable column
    routes bounded, it never silently admits full_frame.
    """
    if resident is None or isinstance(resident, LazySource):
        return {}
    names = set(resident.column_names)
    return {
        col.name: resident.column(col.name) for col in profile_table.columns if col.name in names
    }


def byte_estimate_full_frame_fits(
    profile: Any,
    *,
    caller_sources: dict[str, pa.Table | LazySource],
    table_kinds: dict[str, str],
    budget_bytes: int,
    error_band: float = 0.30,
) -> bool | None:
    """Byte-level full_frame admission signal (Sprint B1b, docs/plans/
    2026-07-10-oom-avoidance-routing-redesign.md §13's conservative-filter
    ruling): whether `_mem_estimate.fits` confirms full_frame fits
    `budget_bytes` for this job's MASK-kind tables.

    Scoped to mask-kind tables only. A caller with a generate-kind table in
    the job (`has_generate_table`) must NOT call this for that job: the
    estimate would omit the generate tables' bytes entirely, which
    under-counts a generate+mask job's true full_frame footprint -- the
    unsafe (under-predicting) direction §13 exists to forbid. That is a
    documented scope limit of B1b (`decide_execution_route`'s
    `use_byte_estimate_routing` keeps such jobs on the pre-existing
    row-count path), not a bug here; a later sprint can extend
    `table_size_spec_from_generate_table` into this signal once its
    `TableConfig` plumbing is threaded through `run_pipeline`.

    Returns the SAME tri-state `fits` does: `True` (confirmed fit),
    `False` (confirmed does not fit), or `None` (UNPRICEABLE -- a
    variable-width mask column with no resident sample to measure, or no
    mask table at all). `decide_execution_route` treats `None` identically
    to `False`: §13 forbids trusting an unknown estimate for full_frame
    admission. A variable-width column only prices when its table is
    RESIDENT in `caller_sources` (a lazy `source_loader` table has nothing
    to sample, and `ColumnProfile` does not yet carry a string-length
    statistic -- see `_mem_estimate_schema`'s module docstring); this is
    the safe direction, not a hole -- an unsampleable string column routes
    bounded until B2's probe recovers the fast path.
    """
    from decoy_engine.execution._mem_estimate import fits
    from decoy_engine.execution._mem_estimate_schema import table_size_spec_from_profile

    mask_tables = [t for t in profile.tables if table_kinds.get(t.name) == "mask"]
    if not mask_tables:
        return None
    specs = tuple(
        table_size_spec_from_profile(
            table, sample=_resident_column_arrays(caller_sources.get(table.name), table)
        )
        for table in mask_tables
    )
    return fits(specs, "full_frame", budget_bytes, error_band=error_band)


def resolve_full_frame_fits_estimate(
    use_byte_estimate_routing: bool,
    profile: Any,
    caller_sources: dict[str, pa.Table | LazySource],
    table_kinds: dict[str, str],
    out_of_core_budget_bytes: int | None,
) -> bool | None:
    """`run_pipeline`'s one call site for the B1b signal: `None` (flag-gated
    no-op -- no cgroup read, no estimator call) when
    `use_byte_estimate_routing` is False, else `byte_estimate_full_frame_fits`
    against the SAME `resolve_budget`-derived budget the out-of-core route
    itself uses (`out_of_core_budget_bytes`, or the detected cgroup/host
    ceiling when left `None`), so one budget serves both decisions.
    """
    if not use_byte_estimate_routing:
        return None
    from decoy_engine.execution.out_of_core import resolve_budget

    budget = resolve_budget(out_of_core_budget_bytes)
    return byte_estimate_full_frame_fits(
        profile,
        caller_sources=caller_sources,
        table_kinds=table_kinds,
        budget_bytes=budget.budget_bytes,
    )


def resolve_probe_recovery(
    use_probe_routing: bool,
    use_byte_estimate_routing: bool,
    profile: Any,
    caller_sources: dict[str, pa.Table | LazySource],
    table_kinds: dict[str, str],
    out_of_core_budget_bytes: int | None,
    full_frame_fits_estimate: bool | None,
    *,
    config: dict[str, Any],
    engine_version: str,
    error_band: float = 0.30,
) -> bool | None:
    """Sprint B2 (docs/plans/2026-07-10-oom-avoidance-routing-redesign.md
    §3.3/§11/§13): `run_pipeline`'s one call site for the probe-recovery
    signal `decide_execution_route`'s `use_probe_routing` flag consumes.

    `use_probe_routing` composes with (never substitutes for)
    `use_byte_estimate_routing`: the probe is a fast-path RECOVERY for a job
    the static estimator over-downgraded (§13), which only makes sense
    inside the B1b conservative-filter branch -- with `use_byte_estimate_
    routing=False` this returns `None` (a flag-gated no-op) regardless of
    `use_probe_routing`.

    Returns `None` in every case where a probe is either unnecessary or out
    of scope, so `decide_execution_route`'s `probe_recovers_full_frame is
    True` check can never fire on a job this function did not actually
    measure:

      - either flag is off;
      - `full_frame_fits_estimate is True` already -- nothing to recover,
        the static estimate already admits full_frame;
      - no mask-kind table exists;
      - any mask-kind table is NOT resident in `caller_sources` (a lazy
        `source_loader` table has no data for `run_pipeline_isolated` to
        serialize into a child process -- same resident-only scope limit
        `byte_estimate_full_frame_fits` documents for sampling, applied
        here to the probe's data requirement instead);
      - the static (unmultiplied) raw-bytes estimate CLEARLY busts the
        budget even under `_probe.MIN_PLAUSIBLE_K_FULL_FRAME` (the most
        favorable real full_frame peak/raw ratio this repo has evidence
        for) -- no real schema could fit, so a probe cannot possibly
        recover it and the ~1-2%-of-a-full-run cost is not worth paying.

    Otherwise runs the two-point probe (`_probe.probe_peak_bytes`) against
    the SAME `resolve_budget`-derived budget `resolve_full_frame_fits_
    estimate` used, and returns `_probe.probe_fits`'s tri-state verdict
    (`True`/`False`/`None`) -- `None` (inconclusive) is surfaced as-is, not
    coerced to `False`: `decide_execution_route` treats anything other than
    `True` identically (route bounded), so the distinction only matters for
    diagnostics, not safety.

    TB-1 scope note: `probe_peak_bytes` below still assumes every mask
    table in `caller_sources` is a resident `pa.Table` (it serializes them
    into a probe subprocess); a `LazySource` entry is not audited for this
    call. Both flags this function requires are default-`False` (TB-1
    keeps them off), so this is not reachable in production this sprint --
    flagged here for TB-5, which enables them, to audit before flip.
    """
    if not (use_probe_routing and use_byte_estimate_routing):
        return None
    if full_frame_fits_estimate is True:
        return None

    mask_tables = [t for t in profile.tables if table_kinds.get(t.name) == "mask"]
    if not mask_tables:
        return None
    if any(t.name not in caller_sources for t in mask_tables):
        return None

    from decoy_engine.execution._mem_estimate import raw_data_bytes
    from decoy_engine.execution._mem_estimate_schema import table_size_spec_from_profile
    from decoy_engine.execution._probe import (
        DEFAULT_PROBE_TIMEOUT_S,
        MIN_PLAUSIBLE_K_FULL_FRAME,
        probe_fits,
        probe_peak_bytes,
        uniqueness_saturation_risk,
    )
    from decoy_engine.execution.out_of_core import resolve_budget

    budget = resolve_budget(out_of_core_budget_bytes)
    specs = tuple(
        table_size_spec_from_profile(
            table, sample=_resident_column_arrays(caller_sources.get(table.name), table)
        )
        for table in mask_tables
    )
    raw = raw_data_bytes(specs)
    if raw.priceable_bytes * MIN_PLAUSIBLE_K_FULL_FRAME > budget.budget_bytes:
        # Clearly busts even under the most favorable real full_frame k this
        # repo has evidence for -- no measured k could bring this under
        # budget, so the probe cost is not worth paying (§13's "skip the
        # probe when the static estimate clearly busts").
        return None

    largest = max(mask_tables, key=lambda t: t.row_count)
    # LOW-2 (dennis's Sprint B2 review): `ColumnProfile.distinct_count` may
    # itself be SAMPLED (see `ColumnProfile.sampled`) rather than a
    # definitive full-scan count -- a sampled undercount could miss a
    # column that is actually near its full-scale cardinality ceiling,
    # silently defeating this uniqueness-saturation guard for exactly the
    # column it exists to catch. Not fixed here (would require threading
    # `sampled` through this dict and deciding what a "possibly saturated"
    # sampled column should do); flagged for B5 telemetry tightening.
    distinct_counts = {
        (t.name, c.name): c.distinct_count
        for t in mask_tables
        for c in t.columns
        if c.distinct_count is not None
    }
    row_counts_at_target = {t.name: t.row_count for t in mask_tables}
    risk_columns = uniqueness_saturation_risk(row_counts_at_target, distinct_counts)

    result = probe_peak_bytes(
        config,
        caller_sources,
        reference_table=largest.name,
        target_rows=largest.row_count,
        uniqueness_risk_columns=risk_columns,
        # MED-1: the physical raw-bytes floor computed from the SAME specs
        # (at full/target scale) used for the pre-filter above -- `None`
        # only when a variable-width mask column could not be priced at
        # all (no floor to apply rather than a wrong one).
        raw_floor_bytes=raw.priceable_bytes if raw.is_priceable else None,
        # MED-2: an explicit probe-appropriate timeout (never the
        # primitive-disabling `None`) and a mem_cap -- the slot budget is a
        # natural cap for a job that is, by construction, only being
        # probed because it might not fit that same budget.
        mem_cap_bytes=budget.budget_bytes,
        timeout_s=DEFAULT_PROBE_TIMEOUT_S,
        engine_version=engine_version,
    )
    return probe_fits(result, budget.budget_bytes, error_band=error_band)


def resolve_execution_route(
    profile: Any,
    *,
    plan: Plan,
    registry: ProviderRegistry,
    graph: RelationshipGraph,
    caller_sources: dict[str, pa.Table | LazySource],
    table_kinds: dict[str, str],
    has_mask_table: bool,
    has_generate_table: bool,
    validators: list[Any],
    fidelity_report: bool,
    vault_writer: Any,
    execution_mode: str,
    resolved_substrate: str,
    out_of_core_threshold_rows: int,
    full_frame_reject_rows: int,
    out_of_core_budget_bytes: int | None,
    use_byte_estimate_routing: bool,
    use_probe_routing: bool,
    config: dict[str, Any],
    engine_version: str,
) -> tuple[str, str]:
    """`run_pipeline`'s single call site bundling every routing SIGNAL in this
    module (out-of-core admission + size, the B1b byte estimate, the B2
    probe recovery) with `decide_execution_route` (`_pipeline_routing.py`)
    itself.

    Purely a wiring consolidation -- `run_pipeline` needs none of the
    intermediate signals (`out_of_core_compatible`, `largest_table_rows`,
    `full_frame_fits_estimate`, ...) for anything other than feeding
    `decide_execution_route`, so bundling them here keeps `_pipeline.py`
    under its own LOC cap without changing any decision logic. Lives in
    THIS module (not `_pipeline_routing.py`, which re-exports it) and
    imports `decide_execution_route` lazily below to avoid a module-level
    import cycle: `_pipeline_routing` already imports this module's
    signals at its own top level.
    """
    from decoy_engine.execution._pipeline_routing import decide_execution_route

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
        caller_sources=caller_sources,
        table_kinds=table_kinds,
        has_mask_table=has_mask_table,
    )
    full_frame_fits_estimate = resolve_full_frame_fits_estimate(
        use_byte_estimate_routing, profile, caller_sources, table_kinds, out_of_core_budget_bytes
    )
    probe_recovers_full_frame = resolve_probe_recovery(
        use_probe_routing,
        use_byte_estimate_routing,
        profile,
        caller_sources,
        table_kinds,
        out_of_core_budget_bytes,
        full_frame_fits_estimate,
        config=config,
        engine_version=engine_version,
    )
    return decide_execution_route(
        profile,
        has_generate_table=has_generate_table,
        has_mask_table=has_mask_table,
        validators=validators,
        fidelity_report=fidelity_report,
        vault_writer=vault_writer,
        execution_mode=execution_mode,
        graph=graph,
        resolved_substrate=resolved_substrate,
        out_of_core_compatible=out_of_core_compatible,
        out_of_core_reject_code=out_of_core_reject_code,
        largest_table_rows=largest_table_rows,
        largest_table_rows_exact=largest_table_rows_exact,
        out_of_core_threshold_rows=out_of_core_threshold_rows,
        full_frame_reject_rows=full_frame_reject_rows,
        use_byte_estimate_routing=use_byte_estimate_routing,
        full_frame_fits_estimate=full_frame_fits_estimate,
        use_probe_routing=use_probe_routing,
        probe_recovers_full_frame=probe_recovers_full_frame,
    )

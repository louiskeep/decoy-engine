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

if TYPE_CHECKING:
    from decoy_engine.plan._types import Plan
    from decoy_engine.profile._types import Profile
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import RelationshipGraph

__all__ = [
    "largest_mask_table_rows",
    "largest_mask_table_rows_from_profile",
    "out_of_core_admission",
    "out_of_core_routing_signals",
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
    caller_sources: dict[str, pa.Table],
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
    caller_sources: dict[str, pa.Table],
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
    caller_sources: dict[str, pa.Table],
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

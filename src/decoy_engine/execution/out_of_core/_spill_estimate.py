"""Conservative disk-footprint upper bound + fail-closed preflight for the
out-of-core route (OOC-D: disk-aware out-of-core routing).

Split out from `_budget.py` rather than appended there: `_budget.py` was
already within a handful of lines of the 600-LOC orchestration cap (CLAUDE.md
"Engineering best practices"), so a sibling module is the correct move (per
that same doc's guidance), not a bloat. This module owns the piece
`_budget.py`'s `check_disk_spill_preflight` docstring flagged as future work:
"once a predicted-spill estimator exists, a caller checks this before
committing to the out-of-core route." `predict_ooc_disk_bytes` is that
estimator; `enforce_ooc_disk_preflight` is the router's call site (wired into
`_pipeline_routing_signals.resolve_execution_route`); `default_ooc_temp_root`
is the single source of truth for where the route's scratch directory lands
absent an explicit `temp_dir`, shared with `_pipeline_route_exec.run_out_of_
core_route`'s runtime-budget threading so the preflight and the runtime cap
check the SAME filesystem.

WHY A CONSERVATIVE UPPER BOUND (not a calibrated point estimate): this gate
is fail-closed. Under-predicting the disk a job needs lets it start and then
die mid-run with a full temp disk -- the runner's own `check_temp_disk_budget`
only catches that AFTER the fact, at the next table boundary, with wall-clock
and partial spill already burned. Over-predicting only costs an early, clean,
actionable reject before any work starts. So this estimator is deliberately an
UPPER bound: it should fail-fast ONLY when even the worst case cannot fit
("definitely doomed"). Jobs that pass but run tight are caught exactly by the
runtime `check_temp_disk_budget` that `run_out_of_core_route` now threads --
so over-rejection is minimized and under-admission (the OOM/disk-full risk) is
eliminated. This mirrors `_mem_estimate.fits`'s own asymmetric posture ("a
false 'fits' risks an OOM kill... a false 'doesn't fit' only costs a
downgrade") and, critically, does NOT repeat the pooled-fixture calibration
error `_mem_estimate.py` already documents (its `K_FULL_FRAME_MEASURED_POOLED`
= 1.156 "NOT schema-invariant" note): a factor fit to one fixture's
dictionary-compression ratio is not a bound for high-cardinality data.

WHAT ACTUALLY LANDS ON THE TEMP FILESYSTEM (the two-term model): the
out-of-core runner does NOT spill whole tables. It spills the FK KEY columns
only -- per incoming edge a DuckDB parent-key temp table + group-by/join radix
partitions, and per outgoing edge a narrow staged-Parquet copy of that table's
key columns (`_stage.py`, `_relation.py`). PAYLOAD columns are masked in Arrow
and stream straight to the sink; they become committed OUTPUT, never transient
spill. So disk pressure is two independent terms:

  SPILL (transient temp, released at job end) =
      key_bytes_per_row * total_rows * max(1, max_per_table_edge_count)
      * SPILL_OVERHEAD

    - key_bytes_per_row: sum of the on-disk widths of every FK-join key column
      in the schema (the columns `_stage.py`/`_relation.py` actually stage).
      Real widths from the profile (`ColumnProfile.max_length` for strings, an
      upper bound on observed cell width; `numpy.dtype(...).itemsize` for
      fixed-width dtypes); a genuinely unknown width prices at the
      `UNKNOWN_WIDTH_CEILING_BYTES` ceiling, never a low default.
    - max_per_table_edge_count: each incoming FK edge holds its own parent-key
      temp table and each outgoing edge stages its own relation, all
      concurrently for the busiest table -- a star/fact table referencing k
      dimensions carries a k multiplier. Derived from the graph
      (`max_per_table_edge_count`), the same edge-fan-in `_pipeline_route_exec.
      _max_concurrent_ooc_instances` reasons about for memory.
    - SPILL_OVERHEAD: DuckDB's larger-than-memory hash join / group-by spills
      radix-PARTITIONED copies of its keys to `temp_directory` (DuckDB "Memory
      Management" / spilling docs), and the runner ADDITIONALLY stages a
      Parquet copy of each outgoing relation's keys -- two independent copies
      of the key bytes, modeled WITHOUT dictionary compression (worst case:
      high-cardinality keys get no dict benefit). See the constant below.

  OUTPUT (committed, persists past job end) =
      sum over tables of (full_output_row_width * table_rows)

    - full_output_row_width: sum of the on-disk widths of ALL that table's
      masked output columns (same width sourcing as keys; no Python object
      overhead -- these are on-disk Parquet bytes, not resident objects).
    - Added ONLY when the committed target and the temp root share a
      filesystem (they then compete for the same free space). When they are on
      DIFFERENT filesystems the output does not compete with the temp root's
      free space, so it is omitted from the temp-disk budget. When the target
      filesystem cannot be determined (no file target in config, or an
      unstattable path) the output term is INCLUDED -- the conservative,
      over-predicting direction.

  disk_needed = (SPILL + OUTPUT) * DISK_SAFETY_MARGIN

ESTABLISHED METHODOLOGY (CLAUDE.md "Core rule for non-trivial engine work"):
the SPILL model follows DuckDB's documented external-memory behavior
(radix-partitioned hash-join / aggregation spilling to `temp_directory`,
re-read like any external-memory operator) rather than a rolled-from-scratch
cost model; the OUTPUT model is just the on-disk Parquet row width times rows.
Both are priced from real profiled widths, never a fixture-fit factor.

CALIBRATION IS A SANITY CHECK, NOT THE MODEL: the measured benchmark
(`decoy-platform` repo `docs/product/benchmarks/scaling-and-capacity.md`,
`fix/ooc-spillable-dedup`, GCP n2-standard-8) is a 3-table parent->child->
grandchild FK chain, 16 payload cols/table, spilling ~5.55 GiB @ 50M and ~11.1
GiB @ 100M total rows with ~5.92 / ~11.8 GiB committed output. The tests assert
this model's prediction is `>=` that measured footprint (the conservative
direction), NOT that it matches a fitted factor -- the point estimate the prior
version pinned (a 0.1x dictionary-compression artifact of that fixture's 4096-
value string pool) is exactly the error class this rewrite removes.

TEMP-ROOT LOW: `default_ooc_temp_root()` reads `tempfile.gettempdir()` once at
call time; a job that later re-points `TMPDIR` between this preflight and the
runner's `mkdtemp` could check a different mount than it spills to. In
practice `run_out_of_core_route` reads the same `default_ooc_temp_root()` for
its runtime budget, so both agree unless the environment mutates underneath
them mid-dispatch -- documented, not defended against.
"""

from __future__ import annotations

import os
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._mem_estimate import is_fixed_width_dtype
from decoy_engine.execution.out_of_core._budget import (
    _nearest_existing_ancestor,
    check_disk_spill_preflight,
)

if TYPE_CHECKING:
    from decoy_engine.profile._types import ColumnProfile, Profile
    from decoy_engine.relationships import RelationshipGraph

__all__ = [
    "DISK_SAFETY_MARGIN",
    "SPILL_OVERHEAD",
    "UNKNOWN_WIDTH_CEILING_BYTES",
    "OocDiskPrediction",
    "default_ooc_temp_root",
    "enforce_ooc_disk_preflight",
    "max_per_table_edge_count",
    "predict_ooc_disk_bytes",
]

# DuckDB's external hash join / aggregation spills radix-PARTITIONED copies of
# its keys to `temp_directory` and the runner separately stages a Parquet copy
# of each outgoing relation's keys -- two independent on-disk copies of the key
# bytes. Modeled WITHOUT dictionary compression on purpose: high-cardinality FK
# keys (the OOM-relevant case) get no dict benefit, so the worst-case temp
# footprint is ~2x the raw key bytes. Deliberately the top of the ~1.5-2.0x
# range: over-predicting the transient temp is the safe direction, and the
# runtime `check_temp_disk_budget` catches anything this misses.
SPILL_OVERHEAD = 2.0

# Upper-bound width for a variable-width column whose real width is genuinely
# unknown (a string column the profiler recorded no `max_length` for -- e.g. an
# all-null sample). 256 bytes covers the ubiquitous SQL VARCHAR(255) free-text
# convention with a byte to spare and is an order of magnitude above ID/code
# widths, so a mis-sized ID never sneaks under it. NEVER a small default: for a
# fail-closed gate the unknown-width fallback must OVER-estimate (a 500-byte
# notes column priced at a 12-byte minimum was the exact under-prediction the
# reviewers blocked).
UNKNOWN_WIDTH_CEILING_BYTES = 256

# Final headroom on the summed two-term footprint. Over-predicting the total
# is the safe direction; the runtime cap is the tight backstop.
DISK_SAFETY_MARGIN = 1.25


@dataclass(frozen=True)
class OocDiskPrediction:
    """The two-term conservative disk footprint for one out-of-core job.

    `spill_bytes` is the transient temp (key columns x rows x edge fan-in x
    overhead); `output_bytes` is the committed on-disk output (0 when the
    output filesystem differs from the temp root, see the module docstring);
    `total_bytes` is `(spill + output) * DISK_SAFETY_MARGIN`, the number the
    preflight compares against free disk. `output_included` records whether the
    output term was added, for diagnostics.
    """

    spill_bytes: int
    output_bytes: int
    total_bytes: int
    output_included: bool


def _column_disk_width_bytes(col: ColumnProfile) -> float:
    """On-disk (Parquet) bytes per row for one column -- an UPPER bound.

    Fixed-width dtypes price at their exact itemsize (`numpy.dtype(label).
    itemsize`, the same table `_mem_estimate._FIXED_WIDTH_DTYPE_BYTES`
    documents). A variable-width (string/object) column prices at its profiled
    `max_length` -- the largest cell width the profiler observed, an upper
    bound on the per-row payload -- or, when the profiler recorded none, at the
    `UNKNOWN_WIDTH_CEILING_BYTES` free-text ceiling. No Python object overhead
    is added: this is on-disk bytes, not resident objects.
    """
    if is_fixed_width_dtype(col.dtype):
        return float(np.dtype(col.dtype).itemsize)
    if col.max_length is not None:
        return float(col.max_length)
    return float(UNKNOWN_WIDTH_CEILING_BYTES)


def max_per_table_edge_count(graph: RelationshipGraph) -> int:
    """The busiest table's concurrent FK-edge count (incoming + outgoing).

    Each incoming FK edge holds its own parent-key temp table and each outgoing
    edge stages its own relation, all live at once for that table, so its temp
    footprint scales with incoming + outgoing edge count -- a star/fact table
    referencing k dimensions carries a k multiplier. Returns at least 1 (a
    single-table or edgeless job still spills its own keys once). This is the
    same edge-fan-in property `_pipeline_route_exec._max_concurrent_ooc_
    instances` reasons about for memory, read here for the disk multiplier.
    """
    incoming: dict[str, int] = defaultdict(int)
    outgoing: dict[str, int] = defaultdict(int)
    for edge in graph.edges:
        outgoing[edge.parent_table] += 1
        incoming[edge.child_table] += 1
    tables = set(incoming) | set(outgoing)
    if not tables:
        return 1
    return max(1, max(incoming[table] + outgoing[table] for table in tables))


def _fk_key_columns(graph: RelationshipGraph) -> set[tuple[str, str]]:
    """Every `(table, column)` that is an FK-join key -- both the parent-key
    and child-FK sides of every edge. These are the columns `_stage.py` /
    `_relation.py` actually stage to the temp filesystem."""
    keys: set[tuple[str, str]] = set()
    for edge in graph.edges:
        for column in edge.parent_columns:
            keys.add((edge.parent_table, column))
        for column in edge.child_columns:
            keys.add((edge.child_table, column))
    return keys


def predict_ooc_disk_bytes(
    profile: Profile,
    *,
    graph: RelationshipGraph,
    table_kinds: dict[str, str],
    include_output: bool,
) -> OocDiskPrediction:
    """The conservative two-term temp-disk upper bound for an out-of-core job.

    See the module docstring for the model. `include_output` is the caller's
    filesystem verdict (does the committed output compete with the temp root's
    free space?) -- kept a pure parameter so this function does no I/O and is
    testable without touching the filesystem; `enforce_ooc_disk_preflight`
    computes it via `os.stat` device comparison. Scoped to MASK-kind tables;
    the out-of-core route is pure-mask-FK, so every streamed table is covered.
    """
    mask_tables = [table for table in profile.tables if table_kinds.get(table.name) == "mask"]
    col_index: dict[tuple[str, str], ColumnProfile] = {
        (table.name, col.name): col for table in mask_tables for col in table.columns
    }
    total_rows = sum(table.row_count for table in mask_tables)

    key_bytes_per_row = sum(
        _column_disk_width_bytes(col_index[key])
        for key in _fk_key_columns(graph)
        if key in col_index
    )
    edge_multiplicity = max_per_table_edge_count(graph)
    spill_bytes = int(key_bytes_per_row * total_rows * edge_multiplicity * SPILL_OVERHEAD)

    output_bytes = 0
    if include_output:
        output_bytes = int(
            sum(
                sum(_column_disk_width_bytes(col) for col in table.columns) * table.row_count
                for table in mask_tables
            )
        )
    total_bytes = int((spill_bytes + output_bytes) * DISK_SAFETY_MARGIN)
    return OocDiskPrediction(
        spill_bytes=spill_bytes,
        output_bytes=output_bytes,
        total_bytes=total_bytes,
        output_included=include_output,
    )


def default_ooc_temp_root() -> Path:
    """Where the out-of-core runner's scratch directory lands absent an
    explicit `temp_dir` (`run_fk_out_of_core`'s own `tempfile.mkdtemp` default,
    which draws from the same `tempfile.gettempdir()` root). Single source of
    truth so this module's preflight and `_pipeline_route_exec.run_out_of_core_
    route`'s runtime-budget threading check the SAME filesystem -- two guards
    silently checking two different mounts could disagree on whether disk is
    sufficient. See the module docstring's TEMP-ROOT LOW note.
    """
    return Path(tempfile.gettempdir())


def _config_target_dirs(config: dict[str, Any]) -> list[Path]:
    """Parent directories of every file target in `config`, for the output-
    filesystem comparison. Non-file / path-less targets are skipped."""
    dirs: list[Path] = []
    for spec in (config.get("targets") or {}).values():
        if isinstance(spec, dict):
            path = spec.get("path")
            if isinstance(path, str) and path:
                dirs.append(Path(path).parent)
    return dirs


def _output_shares_temp_filesystem(config: dict[str, Any], temp_root: Path) -> bool:
    """Does the committed output land on the SAME filesystem as `temp_root`?

    Compares `os.stat(...).st_dev` of the temp root against each file target's
    directory (walking to the nearest existing ancestor for a not-yet-created
    path). Returns True (include the output term -- the conservative,
    over-predicting direction) when there is no file target to compare, or when
    any device cannot be stat'd, or when any target shares the temp device.
    Only returns False when EVERY target is provably on a different device.
    """
    target_dirs = _config_target_dirs(config)
    if not target_dirs:
        return True
    try:
        temp_dev = os.stat(_nearest_existing_ancestor(temp_root)).st_dev
    except OSError:
        return True
    for directory in target_dirs:
        try:
            if os.stat(_nearest_existing_ancestor(directory)).st_dev == temp_dev:
                return True
        except OSError:
            return True
    return False


def enforce_ooc_disk_preflight(
    profile: Profile,
    *,
    graph: RelationshipGraph,
    table_kinds: dict[str, str],
    config: dict[str, Any],
    temp_dir: Path | None = None,
) -> None:
    """Fail-closed disk gate for a job just routed to `out_of_core`
    (`_pipeline_routing_signals.resolve_execution_route`'s one call site,
    checked AFTER `decide_execution_route` has already picked the route).

    No-op when there is no mask table to price: every `decide_execution_route`
    branch that returns `"out_of_core"` already implies `has_mask_table`, so
    this is belt-and-suspenders, not a live skip.

    This is a BACKSTOP for OOC-ELIGIBLE jobs ONLY: it runs strictly after
    `decide_execution_route` has already chosen `out_of_core` for THIS job, so
    it can never reroute an OOC-INCOMPATIBLE job -- those are rejected earlier,
    inside `decide_execution_route` itself (the cyclic-FK / unsupported-strategy
    branches), a code path this function never touches and never runs for.

    Raises `ExecutionError` (`out_of_core_disk_preflight_insufficient`) with an
    ACTIONABLE message -- the predicted GiB, the checked directory, the row
    count, and the free GiB actually available -- when the conservative
    worst-case footprint does not fit, so an operator reads exactly what to do
    (free disk space, or reduce the row count). Because the estimate is an
    UPPER bound, reaching this raise means even the worst case is doomed; a job
    that passes but runs tight is caught by the runtime `check_temp_disk_budget`
    instead.
    """
    mask_tables = [table for table in profile.tables if table_kinds.get(table.name) == "mask"]
    if not mask_tables:
        return
    root = temp_dir if temp_dir is not None else default_ooc_temp_root()
    include_output = _output_shares_temp_filesystem(config, root)
    prediction = predict_ooc_disk_bytes(
        profile, graph=graph, table_kinds=table_kinds, include_output=include_output
    )
    result = check_disk_spill_preflight(root, predicted_spill_bytes=prediction.total_bytes)
    if result.ok:
        return
    total_rows = sum(table.row_count for table in mask_tables)
    predicted_gib = result.predicted_bytes / (1024**3)
    free_gib = result.free_bytes / (1024**3)
    raise ExecutionError(
        code="out_of_core_disk_preflight_insufficient",
        message=(
            f"out-of-core route needs ~{predicted_gib:.1f} GiB free temp disk at "
            f"{root} for {total_rows:,} rows, but only {free_gib:.1f} GiB is "
            "available; free disk space or reduce the row count."
        ),
    )

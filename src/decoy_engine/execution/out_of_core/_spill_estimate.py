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

MASKED-SIDE WIDTHS (not source widths): what stages/commits to disk is the
MASKED value, not the source. A `hash`-masked int64 key sources 8 bytes but
stages a 64-char hex digest (`kernel/_scalar.py::hash_array`,
`derive(...).hex()`, 32-byte HMAC-SHA256 -> 64 hex chars, untruncated by
default). So every column prices at `max(source_width, strategy_output_width)`
-- the masked width can only be >= the source, never smaller for sizing
purposes. `strategy_output_width` reads the column's config:

  - hash: the digest hex width -- the `truncate` provider-config param if a
    positive int, else HASH_DIGEST_HEX_BYTES (64).
  - redact: `len(redact_with)` (the replacement token; default "REDACTED").
  - passthrough / truncate / fpe (format-preserving or shrinking): source
    width -- these never widen a value, so `max(source, 0)` keeps the source.
  - any OTHER strategy whose output width is not cleanly derivable from config
    (text_redact / text_mask / categorical / code_set / bucket_perturb, or a
    future strategy): the UNKNOWN_WIDTH_CEILING_BYTES ceiling, the same
    conservative default an unknown source width takes. Documented fallback,
    not a guess pretending to be exact.

WHAT ACTUALLY LANDS ON THE TEMP FILESYSTEM (the two-term model): the
out-of-core runner does NOT spill whole tables. It spills the FK KEY columns
only -- per incoming edge a DuckDB parent-key temp table + group-by/join radix
partitions, per outgoing edge a narrow staged-Parquet copy of the key columns
(`_stage.py`, `_relation.py::_staging_schema`). The staged/join key is a
STRING TOKEN (`execution/_fk_keys.py::fk_join_key`, `\x00STR:{len}:{value}`
tuple-framed), and an `__decoy_row_nr` int64 column is staged alongside it.
PAYLOAD columns are masked in Arrow and stream straight to the sink; they
become committed OUTPUT, never transient spill. So disk pressure is two
independent terms, EACH scaled by ITS OWN table's row count (never a
cross-product of all key widths x all rows, which would inflate quadratically
in schema breadth):

  SPILL (transient temp, released at job end) =
      sum over tables of (
          (row_nr_bytes + sum over that table's FK-key columns of
              staged_key_token_bytes(masked_width))
          * table_rows
      ) * max(1, max_per_table_edge_count) * SPILL_OVERHEAD

    - staged_key_token_bytes = masked_width + FK_TOKEN_FRAMING_BYTES, floored
      at MIN_KEY_TOKEN_BYTES (covers the ~28-byte int-key token). ROW_NR_BYTES
      (8) is added once per table with staged keys.
    - max_per_table_edge_count: each incoming FK edge holds its own parent-key
      temp table and each outgoing edge stages its own relation, all
      concurrently for the busiest table -- a star/fact table referencing k
      dimensions carries a k multiplier. From `max_per_table_edge_count`, the
      same edge-fan-in `_pipeline_route_exec._max_concurrent_ooc_instances`
      reasons about for memory.
    - SPILL_OVERHEAD: DuckDB's larger-than-memory hash join / group-by spills
      radix-PARTITIONED copies of its keys to `temp_directory` (DuckDB "Memory
      Management" / spilling docs), and the runner ADDITIONALLY stages a
      Parquet copy of each outgoing relation's keys -- two independent copies
      of the key bytes, modeled WITHOUT dictionary compression (worst case:
      high-cardinality keys get no dict benefit). See the constant below.

  OUTPUT (committed, persists past job end) =
      sum over tables of (
          sum over ALL that table's columns of masked_width * table_rows
      )

    - Added ONLY when the committed target and the temp root share a
      filesystem (they then compete for the same free space). When they are on
      DIFFERENT filesystems the output does not compete with the temp root's
      free space, so it is omitted. When the target filesystem cannot be
      determined (no file target in config, or an unstattable path) the output
      term is INCLUDED -- the conservative, over-predicting direction.

  disk_needed = (SPILL + OUTPUT) * DISK_SAFETY_MARGIN

ESTABLISHED METHODOLOGY (CLAUDE.md "Core rule for non-trivial engine work"):
the SPILL model follows DuckDB's documented external-memory behavior
(radix-partitioned hash-join / aggregation spilling to `temp_directory`)
rather than a rolled-from-scratch cost model; the OUTPUT model is just the
on-disk Parquet row width times rows. Both price from real profiled widths and
config-derived masked widths, never a fixture-fit factor.

CALIBRATION IS A SANITY CHECK, NOT THE MODEL: the measured benchmark
(`decoy-platform` repo `docs/product/benchmarks/scaling-and-capacity.md`,
`fix/ooc-spillable-dedup`, GCP n2-standard-8) is a 3-table parent->child->
grandchild FK chain, 16 payload cols/table, hash-masked keys, spilling ~5.55
GiB @ 50M and ~11.1 GiB @ 100M total rows with ~5.92 / ~11.8 GiB committed
output. The tests assert this model's prediction is `>=` that measured
footprint (the conservative direction), NOT that it matches a fitted factor.

WIDTHS ARE SAMPLE-RELATIVE: `ColumnProfile.max_length` is the max string
length the profiler OBSERVED, which under bounded/sampled profiling is a
sample-relative max, not a guaranteed absolute upper bound on every unseen
row. That residual risk is intentionally cushioned by SPILL_OVERHEAD (2.0) and
DISK_SAFETY_MARGIN (1.25) on top, and by the runtime `check_temp_disk_budget`
backstop -- the estimate does not need to be exact, only conservative.

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

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._mem_estimate import _FIXED_WIDTH_DTYPE_BYTES, is_fixed_width_dtype
from decoy_engine.execution.out_of_core._budget import (
    _nearest_existing_ancestor,
    check_disk_spill_preflight,
)

if TYPE_CHECKING:
    from decoy_engine.profile._types import ColumnProfile, Profile
    from decoy_engine.relationships import RelationshipGraph

__all__ = [
    "DISK_SAFETY_MARGIN",
    "HASH_DIGEST_HEX_BYTES",
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
# unknown (a string column the profiler recorded no `max_length` for, or a
# strategy whose output width is not derivable from config). 256 bytes covers
# the ubiquitous SQL VARCHAR(255) free-text convention with a byte to spare and
# is an order of magnitude above ID/code widths, so a mis-sized ID never sneaks
# under it. NEVER a small default: for a fail-closed gate the unknown-width
# fallback must OVER-estimate (a 500-byte notes column priced at a 12-byte
# minimum was the exact under-prediction the reviewers blocked).
UNKNOWN_WIDTH_CEILING_BYTES = 256

# `derive(...).hex()` is a 32-byte HMAC-SHA256 digest -> 64 hex chars
# (`determinism/_derive.py::derive`, `kernel/_scalar.py::hash_array`). This is
# the hash strategy's default masked-value width; a positive `truncate` param
# shortens it.
HASH_DIGEST_HEX_BYTES = 64

# The redact strategy's default replacement token when `redact_with` is unset
# (`execution/_strategies/_redact.py::_DEFAULT_REDACT_WITH`).
_DEFAULT_REDACT_WITH = "REDACTED"

# FK join key framing: `fk_join_key` tags each key `\x00STR:{len}:{value}` and
# `fk_join_key_tuple` length-prefixes each component (`execution/_fk_keys.py`),
# so a staged string token is roughly the value width plus this framing. The
# floor covers the ~28-byte int-key token (`\x00INT:{digits}` + tuple framing)
# for a narrow numeric key whose value width alone would under-count the token.
FK_TOKEN_FRAMING_BYTES = 16
MIN_KEY_TOKEN_BYTES = 28

# The `__decoy_row_nr` int64 column staged alongside the key columns
# (`_relation.py::_staging_schema`), one per table's staged key set.
ROW_NR_BYTES = 8

# Strategies that never WIDEN a value: passthrough and fpe preserve width,
# truncate shrinks it. Priced at the source width (`max(source, 0)`).
_WIDTH_PRESERVING_STRATEGIES = frozenset({"passthrough", "truncate", "fpe"})

# Final headroom on the summed two-term footprint. Over-predicting the total
# is the safe direction; the runtime cap is the tight backstop.
DISK_SAFETY_MARGIN = 1.25


@dataclass(frozen=True)
class OocDiskPrediction:
    """The two-term conservative disk footprint for one out-of-core job.

    `spill_bytes` is the transient temp (per-table key tokens x rows x edge
    fan-in x overhead); `output_bytes` is the committed on-disk output (0 when
    the output filesystem differs from the temp root, see the module
    docstring); `total_bytes` is `(spill + output) * DISK_SAFETY_MARGIN`, the
    number the preflight compares against free disk. `output_included` records
    whether the output term was added, for diagnostics.
    """

    spill_bytes: int
    output_bytes: int
    total_bytes: int
    output_included: bool


def _source_disk_width_bytes(col: ColumnProfile) -> float:
    """On-disk (Parquet) SOURCE bytes per row for one column -- an upper bound.

    Fixed-width dtypes price at their exact itemsize via the single source of
    truth `_mem_estimate._FIXED_WIDTH_DTYPE_BYTES` (the same table
    `is_fixed_width_dtype` gates on). A variable-width (string/object) column
    prices at its profiled `max_length` (a sample-relative max, see the module
    docstring) or, when the profiler recorded none, the
    `UNKNOWN_WIDTH_CEILING_BYTES` free-text ceiling. No Python object overhead:
    this is on-disk bytes, not resident objects.
    """
    if is_fixed_width_dtype(col.dtype):
        return float(_FIXED_WIDTH_DTYPE_BYTES[col.dtype])
    if col.max_length is not None:
        return float(col.max_length)
    return float(UNKNOWN_WIDTH_CEILING_BYTES)


def _provider_config_and_strategy(
    col_cfg: dict[str, Any] | None,
) -> tuple[str | None, dict[str, Any]]:
    """`(strategy, params)` for one column's config entry.

    Params come from the nested `provider_config` (a dict, or an iterable of
    pairs) merged OVER the entry's own top-level keys, so both the modern
    nested shape (`{"strategy": "hash", "provider_config": {"truncate": 16}}`)
    and a flat `{"strategy": "redact", "redact_with": "X"}` resolve the param.
    """
    if col_cfg is None:
        return None, {}
    strategy = col_cfg.get("strategy")
    raw = col_cfg.get("provider_config")
    nested: dict[str, Any] = {}
    if isinstance(raw, dict):
        nested = raw
    elif raw:
        try:
            nested = dict(raw)
        except (TypeError, ValueError):
            nested = {}
    flat = {k: v for k, v in col_cfg.items() if k not in ("provider_config", "columns")}
    return (strategy if isinstance(strategy, str) else None), {**flat, **nested}


def _strategy_output_width_bytes(strategy: str | None, params: dict[str, Any]) -> float:
    """The strategy's characteristic MASKED output width, or 0 for a
    source-preserving/shrinking strategy (the caller takes `max(source, this)`).

    See the module docstring's "MASKED-SIDE WIDTHS" section for the mapping and
    why an unmapped strategy takes the conservative ceiling.
    """
    if strategy == "hash":
        truncate = params.get("truncate")
        if isinstance(truncate, int) and truncate > 0:
            return float(truncate)
        return float(HASH_DIGEST_HEX_BYTES)
    if strategy == "redact":
        token = params.get("redact_with", _DEFAULT_REDACT_WITH)
        return float(len(str(token))) if token is not None else 0.0
    if strategy is None or strategy in _WIDTH_PRESERVING_STRATEGIES:
        return 0.0
    # text_redact / text_mask / categorical / code_set / bucket_perturb / any
    # future strategy: output width not cleanly derivable from config -> ceiling.
    return float(UNKNOWN_WIDTH_CEILING_BYTES)


def _masked_disk_width_bytes(col: ColumnProfile, col_cfg: dict[str, Any] | None) -> float:
    """On-disk width of the MASKED value for one column: `max(source_width,
    strategy_output_width)` -- the masked value is what actually stages/commits,
    and it is never narrower than the source for sizing purposes."""
    source = _source_disk_width_bytes(col)
    strategy, params = _provider_config_and_strategy(col_cfg)
    return max(source, _strategy_output_width_bytes(strategy, params))


def _staged_key_token_bytes(masked_width: float) -> float:
    """On-disk width of one staged FK join token for a key of `masked_width`
    -- the masked value plus `fk_join_key` framing, floored at the int-key
    token size so a narrow numeric key does not under-count."""
    return max(masked_width + FK_TOKEN_FRAMING_BYTES, float(MIN_KEY_TOKEN_BYTES))


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


def _strategy_index(config: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """`{(table, column): column_config}` from the user config's `tables`
    block, so per-column masked widths can be derived. Columns absent here
    (unconfigured passthrough) simply resolve to source widths."""
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for table in config.get("tables") or []:
        if not isinstance(table, dict):
            continue
        table_name = table.get("name") or table.get("table")
        if not isinstance(table_name, str):
            continue
        for col in table.get("columns") or []:
            if isinstance(col, dict):
                col_name = col.get("name") or col.get("column")
                if isinstance(col_name, str):
                    index[(table_name, col_name)] = col
    return index


def predict_ooc_disk_bytes(
    profile: Profile,
    *,
    graph: RelationshipGraph,
    table_kinds: dict[str, str],
    config: dict[str, Any],
    include_output: bool,
) -> OocDiskPrediction:
    """The conservative two-term temp-disk upper bound for an out-of-core job.

    See the module docstring for the model. Widths are MASKED widths derived
    from `config` (hash digest, redact token, etc.), each term scaled by its
    OWN table's row count. `include_output` is the caller's filesystem verdict
    (does the committed output compete with the temp root's free space?) --
    kept a pure parameter so this function does no I/O and is testable without
    touching the filesystem; `enforce_ooc_disk_preflight` computes it via
    `os.stat` device comparison. Scoped to MASK-kind tables; the out-of-core
    route is pure-mask-FK, so every streamed table is covered.
    """
    mask_tables = [table for table in profile.tables if table_kinds.get(table.name) == "mask"]
    strategy_index = _strategy_index(config)
    key_columns = _fk_key_columns(graph)
    edge_multiplicity = max_per_table_edge_count(graph)

    def masked_width(table_name: str, col: ColumnProfile) -> float:
        return _masked_disk_width_bytes(col, strategy_index.get((table_name, col.name)))

    # SPILL: per-table key tokens x that table's own rows (never a cross-product
    # of all key widths x all rows), then the edge fan-in and overhead.
    spill = 0.0
    for table in mask_tables:
        key_cols = [col for col in table.columns if (table.name, col.name) in key_columns]
        if not key_cols:
            continue
        table_key_bytes = float(ROW_NR_BYTES) + sum(
            _staged_key_token_bytes(masked_width(table.name, col)) for col in key_cols
        )
        spill += table_key_bytes * table.row_count
    spill_bytes = int(spill * edge_multiplicity * SPILL_OVERHEAD)

    # OUTPUT: full masked row width x that table's own rows.
    output_bytes = 0
    if include_output:
        output_bytes = int(
            sum(
                sum(masked_width(table.name, col) for col in table.columns) * table.row_count
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
        profile, graph=graph, table_kinds=table_kinds, config=config, include_output=include_output
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

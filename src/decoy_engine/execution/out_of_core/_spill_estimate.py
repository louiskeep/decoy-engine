"""Schema-derived out-of-core spill estimator + fail-closed disk preflight
(OOC-D: disk-aware out-of-core routing).

Split out from `_budget.py` rather than appended there: `_budget.py` was
already within a handful of lines of the 600-LOC orchestration cap (CLAUDE.md
"Engineering best practices") when this landed, so a sibling module is the
correct move (per that same doc's guidance), not a bloat. This module owns
the piece `_budget.py`'s `check_disk_spill_preflight` docstring flagged as
future work: "once a predicted-spill estimator exists, a caller checks this
before committing to the out-of-core route." `predict_ooc_spill_bytes` is
that estimator; `enforce_ooc_disk_preflight` is the router's call site (wired
into `_pipeline_routing_signals.resolve_execution_route`); `default_ooc_
temp_root` is the single source of truth for where the route's scratch
directory lands absent an explicit `temp_dir`, shared with `_pipeline_route_
exec.run_out_of_core_route`'s runtime-budget threading so the preflight and
the runtime cap check the SAME filesystem.

CALIBRATION (established-methodology rule, CLAUDE.md "Core rule for
non-trivial engine work"): reuses `_mem_estimate.raw_data_bytes` -- the SAME
schema-derived per-row byte model `_pipeline_routing_signals.byte_estimate_
full_frame_fits` already prices full_frame/out_of_core ADMISSION with --
rather than inventing a second cost table for SPILL. `SPILL_FACTOR` converts
that RAW (in-memory, per-cell-priced) byte figure into a SPILL (on-disk,
DuckDB/Parquet-staged) byte figure, calibrated against a real measurement:
`docs/product/benchmarks/scaling-and-capacity.md` (decoy-platform repo,
`fix/ooc-spillable-dedup` @ `6472d6a`, GCP n2-standard-8 harness) measured a
3-table parent->child->grandchild FK chain, 16 payload columns/table, 2%
orphans (`tests/perf_fixtures/fk_relational.py`'s `build_fk_relational`
shape), spilling ~0.11 GiB per 1M TOTAL rows -- 5.55 GiB @ 50M, 11.1 GiB @
100M, FLAT across every RAM cap tested (4-24 GB), confirming spill scales
with row volume alone, not the memory budget (the doc's own "Observations"
section).

Reconstructing that calibration shape as a `TableSizeSpec` (parent: 1 short
numeric-string key + 16 pooled 12-byte-average payload strings; child: 2 such
keys + 16 payload; grandchild: 1 key + 16 payload -- all `object`-dtype,
matching the fixture's `_string_pool(width=12)` filler and `_keys()` numeric
suffix) and pricing it through `raw_data_bytes` yields 1,192 raw bytes per
TOTAL row (rows summed across all three tables, matching the benchmark doc's
own "Total rows = 3 x rows/table" convention). The measured spill is 119.19
bytes/row (5.55 GiB / 50M rows, and independently 11.1 GiB / 100M rows --
the two points agree, confirming linearity). 119.19 / 1,192 = 0.09999 --
`SPILL_FACTOR` is that ratio rounded UP to a clean constant (0.1), the same
"pin the measured value rounded up" convention `_mem_estimate.K_INTERCEPT_
BYTES` documents for its own calibrated constants. The `raw_data_bytes` model
prices every string cell independently (an 8-byte pointer + a 49-byte CPython
object header + the payload, `_mem_estimate._STR_OBJECT_OVERHEAD_BYTES`)
while a DuckDB Parquet spill stores only encoded payload bytes with no
per-cell Python object overhead -- RAW bytes structurally over-count a
spill's true footprint by roughly that per-cell overhead ratio, which is
exactly why `SPILL_FACTOR` is well under 1.0. Folding the whole ratio into
one measured constant (rather than modeling Parquet's on-disk encoding from
first principles) is this module's whole reason to exist: get the
destination NUMBER right for a routing gate, not reproduce DuckDB's storage
format.

SAFETY MARGIN: `SPILL_SAFETY_MARGIN` (1.5x) is applied ON TOP of the
already-calibrated `SPILL_FACTOR`, never folded into one combined constant,
so the measured ratio and the deliberate safety pad stay independently
auditable and re-calibratable. Under-predicting a spill risks a mid-run
disk-full failure -- the runner's own `check_temp_disk_budget` only catches
that AFTER the fact, at the next table boundary, by which point the job has
already burned wall-clock time and partial spill; over-predicting only costs
an early, clean, actionable reject before any work starts. That asymmetry
(mid-run failure vs. early reject) is exactly why this estimator biases
toward over-prediction, the same posture `_mem_estimate.fits`'s docstring
documents for its own asymmetric error band ("a false 'fits' risks an OOM
kill... a false 'doesn't fit' only costs a downgrade").

UNPRICEABLE COLUMNS: a lazy (`source_loader`) out-of-core job has no resident
sample to measure a variable-width column's real width from (`table_size_
spec_from_profile` marks it `unpriceable=True` absent a `sample`/`declared_
widths` entry) -- exactly the shape the out-of-core route exists to serve,
so "skip the preflight" is not an acceptable response. `_price_unpriceable_
columns` prices any unpriceable variable-width column at `_UNPRICEABLE_
FALLBACK_WIDTH_BYTES` (12 bytes: the calibration shape's OWN payload width)
rather than the unsafe alternative of silently dropping it from the sum
(0 bytes would under-count, exactly the direction this whole estimator biases
against).
"""

from __future__ import annotations

import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._mem_estimate import ColumnSizeSpec, TableSizeSpec, raw_data_bytes
from decoy_engine.execution.out_of_core._budget import (
    DiskSpillPreflight,
    check_disk_spill_preflight,
)

if TYPE_CHECKING:
    from decoy_engine.profile._types import Profile

__all__ = [
    "SPILL_FACTOR",
    "SPILL_SAFETY_MARGIN",
    "default_ooc_temp_root",
    "enforce_ooc_disk_preflight",
    "ooc_disk_spill_preflight",
    "predict_ooc_spill_bytes",
]

# See module docstring "CALIBRATION": the measured spill/raw-bytes ratio for
# the calibration shape is 0.09999, rounded UP to this clean constant.
SPILL_FACTOR = 0.1

# See module docstring "SAFETY MARGIN": applied on top of SPILL_FACTOR, kept
# as an independent constant rather than folded into it.
SPILL_SAFETY_MARGIN = 1.5

# See module docstring "UNPRICEABLE COLUMNS": the calibration shape's own
# pooled payload-string width (`tests/perf_fixtures/fk_relational.py`'s
# `_string_pool(width=12)`) -- an unsampled column costs what a KNOWN column
# of this route's calibration schema does, never zero.
_UNPRICEABLE_FALLBACK_WIDTH_BYTES = 12.0


def _price_unpriceable_columns(table: TableSizeSpec) -> TableSizeSpec:
    """`table` with every `unpriceable=True` column priced at the fallback
    width instead of silently dropped -- see module docstring "UNPRICEABLE
    COLUMNS". Returns `table` unchanged (no copy) when nothing needs pricing.
    """
    if not any(column.unpriceable for column in table.columns):
        return table
    priced_columns = tuple(
        ColumnSizeSpec(
            name=column.name,
            dtype=column.dtype,
            string_width_bytes=_UNPRICEABLE_FALLBACK_WIDTH_BYTES,
        )
        if column.unpriceable
        else column
        for column in table.columns
    )
    return TableSizeSpec(name=table.name, row_count=table.row_count, columns=priced_columns)


def predict_ooc_spill_bytes(tables: Sequence[TableSizeSpec]) -> int:
    """Predict the out-of-core route's transient temp-disk spill for `tables`.

    `spill ~= raw_data_bytes(tables) * SPILL_FACTOR * SPILL_SAFETY_MARGIN` --
    see the module docstring for how `SPILL_FACTOR` was calibrated and why
    the safety margin is applied on top rather than folded in. Each
    `TableSizeSpec` in `tables` carries both the schema (per-column dtype and
    width) and its own row count, so the "total rows" the calibration is
    expressed in (the benchmark doc's "Total rows = 3 x rows/table") is
    simply the sum baked into `raw_data_bytes`'s own `rows * per-column cost`
    accumulation -- this function never needs to compute it as a separate
    input. Returns 0 for an empty `tables` sequence (no schema to price).
    """
    if not tables:
        return 0
    priced_tables = tuple(_price_unpriceable_columns(table) for table in tables)
    raw = raw_data_bytes(priced_tables)
    return int(raw.priceable_bytes * SPILL_FACTOR * SPILL_SAFETY_MARGIN)


def default_ooc_temp_root() -> Path:
    """Where the out-of-core runner's scratch directory lands absent an
    explicit `temp_dir` (`run_fk_out_of_core`'s own `tempfile.mkdtemp`
    default, which draws from the same `tempfile.gettempdir()` root). Single
    source of truth so this module's preflight and `_pipeline_route_exec.
    run_out_of_core_route`'s runtime-budget threading check the SAME
    filesystem -- two guards silently checking two different mounts could
    disagree on whether disk is sufficient.
    """
    return Path(tempfile.gettempdir())


def ooc_disk_spill_preflight(temp_dir: Path, tables: Sequence[TableSizeSpec]) -> DiskSpillPreflight:
    """`check_disk_spill_preflight` fed by this module's own estimator -- the
    wiring `_budget.py`'s docstring flagged as future work ("once a
    predicted-spill estimator exists, a caller checks this before committing
    to the out-of-core route")."""
    return check_disk_spill_preflight(
        temp_dir, predicted_spill_bytes=predict_ooc_spill_bytes(tables)
    )


def enforce_ooc_disk_preflight(
    profile: Profile, *, table_kinds: dict[str, str], temp_dir: Path | None = None
) -> None:
    """Fail-closed disk-spill gate for a job just routed to `out_of_core`
    (`_pipeline_routing_signals.resolve_execution_route`'s one call site,
    checked AFTER `decide_execution_route` has already picked the route).

    Scoped to MASK-kind tables in `profile.tables`, mirroring `byte_estimate_
    full_frame_fits`'s own mask-table filter -- the out-of-core route is
    pure-mask-FK by construction (`_sequential_eligible`), so this never
    misses a table the route will actually stream. No-op when there is no
    mask table to price: every `decide_execution_route` branch that returns
    `"out_of_core"` already implies `has_mask_table`, so this is
    belt-and-suspenders, not a live skip.

    This is a BACKSTOP for OOC-ELIGIBLE jobs ONLY: it runs strictly after
    `decide_execution_route` has already chosen `out_of_core` for THIS job,
    so it can never reroute an OOC-INCOMPATIBLE job -- those are rejected
    earlier, inside `decide_execution_route` itself (the cyclic-FK /
    unsupported-strategy branches), a completely separate code path this
    function never touches and never runs for.

    Raises `ExecutionError` (`out_of_core_disk_preflight_insufficient`) with
    an ACTIONABLE message -- the predicted GB, the checked directory, the row
    count, and the free GB actually available -- when the predicted spill
    would not fit, so an operator reads exactly what to do (free disk space,
    or reduce the row count) without re-deriving it from a bare byte count.
    """
    from decoy_engine.execution._mem_estimate_schema import table_size_spec_from_profile

    mask_tables = [table for table in profile.tables if table_kinds.get(table.name) == "mask"]
    if not mask_tables:
        return
    tables = tuple(table_size_spec_from_profile(table) for table in mask_tables)
    total_rows = sum(table.row_count for table in tables)
    root = temp_dir if temp_dir is not None else default_ooc_temp_root()
    result = ooc_disk_spill_preflight(root, tables)
    if result.ok:
        return
    predicted_gb = result.predicted_bytes / (1024**3)
    free_gb = result.free_bytes / (1024**3)
    raise ExecutionError(
        code="out_of_core_disk_preflight_insufficient",
        message=(
            f"out-of-core route needs ~{predicted_gb:.1f} GB free temp disk at "
            f"{root} for {total_rows:,} rows, but only {free_gb:.1f} GB is "
            "available; free disk space or reduce the row count."
        ),
    )

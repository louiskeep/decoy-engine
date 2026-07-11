"""Config/sources down-scaling helper for the B2 micro-probe (OOM-avoidance
routing redesign, `docs/plans/2026-07-10-oom-avoidance-routing-redesign.md`
§3.3, corrected per §11).

Pure and side-effect free: this module never touches a subprocess or reads
memory -- `_probe.py` is the one that calls `run_pipeline_isolated` on the
job this module produces. Split out to keep `_probe.py` (which owns the
two-point fit + guards) under the 600-LOC orchestration cap (CLAUDE.md
"Engineering best practices").

The scaling rule (§11's correction on §3.3): scale EVERY table by the SAME
fraction, never a single table in isolation. A relationship-bearing job's
child tables typically have a fixed fan-out per parent row (e.g. 5 orders
per customer); scaling only the parent (or only the child) would change
that ratio and measure a shape the full-scale job never has. Scaling all
tables by one fraction preserves each table's ROW-COUNT RATIO to the others
at every probe point, which is exactly what makes the two probe points
comparable and their slope meaningful.

That guarantee is about the RATIO of row counts, not referential (FK)
closure (dennis's Sprint B2 MED-3 finding): `downscale_sources` head-slices
each table independently, so a sliced child row's FK key can fall outside
the sliced parent's key range (e.g. a child table not sorted to align with
its parent's row order). This is safe in practice for the peak-RSS
measurement the probe needs, for two independent reasons: masking is
HKDF-stateless (a row's masked value never depends on any OTHER row, FK-
matched or not), so peak RSS tracks row COUNT per table regardless of
whether keys happen to resolve; and `_probe.probe_peak_bytes`'s
`raw_floor_bytes` physical floor backstops any residual risk of an
under-prediction regardless of its cause. See
`tests/unit/execution/test_probe.py::TestFKRepresentativenessRealIntegration`
for a real-subprocess test against a deliberately NON-row-aligned FK
distribution.

Two independent halves of one job need scaling:

  - GENERATE-kind tables carry their row count directly in `config`
    (`TableConfig.row_count`) -- `downscale_config` scales that field.
  - MASK-kind tables get their row count from RESIDENT source data
    (`sources[table_name]`), not from `config` -- `downscale_sources`
    slices the resident `pa.Table` instead. A lazy (`source_loader`) mask
    table has no resident data to slice and is out of scope for the probe
    entirely (`_probe.py`'s caller enforces this; see its module docstring).

A "sane floor" (`DEFAULT_PROBE_FLOOR_ROWS`) keeps every probe run above the
row count where FIXED per-job overhead (interpreter start, imports, Faker
pool init, DuckDB init) would dominate the measured peak and swamp the
per-row signal the two-point fit needs -- §11's "the ~1% runtime claim
ignores fixed setup, which dominates at 100k" correction.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa

__all__ = [
    "DEFAULT_PROBE_FLOOR_ROWS",
    "ScaledJob",
    "downscale_config",
    "downscale_job",
    "downscale_sources",
    "scale_row_count",
]

# A probe below this many rows risks measuring mostly fixed setup cost
# (interpreter/import/Faker/DuckDB init) rather than a real per-row slope
# (§11). A few thousand rows is cheap (still ~1-2% of a multi-million-row
# target) while keeping the per-row allocation pattern dominant in the
# measurement. Not calibrated against a specific benchmark -- a documented
# starting point, tightenable once B5 telemetry exists.
DEFAULT_PROBE_FLOOR_ROWS = 2_000


def scale_row_count(
    row_count: int, fraction: float, *, floor_rows: int = DEFAULT_PROBE_FLOOR_ROWS
) -> int:
    """`row_count` scaled by `fraction`, floored at `floor_rows`, never
    exceeding `row_count` itself (a probe must never ask for MORE rows than
    the real table/config has).

    The floor and the row_count cap can conflict (a table smaller than
    `floor_rows`) -- the row_count cap wins, since there is nothing to floor
    UP to beyond what actually exists.
    """
    if row_count < 0:
        raise ValueError(f"row_count must be >= 0, got {row_count}.")
    if not 0.0 < fraction <= 1.0:
        raise ValueError(f"fraction must be in (0, 1], got {fraction}.")
    if floor_rows < 0:
        raise ValueError(f"floor_rows must be >= 0, got {floor_rows}.")
    scaled = round(row_count * fraction)
    floored = max(scaled, min(floor_rows, row_count))
    return min(floored, row_count)


def downscale_config(
    config: dict[str, Any], fraction: float, *, floor_rows: int = DEFAULT_PROBE_FLOOR_ROWS
) -> dict[str, Any]:
    """A deep-copied `config` with every GENERATE-kind table's `row_count`
    scaled by `fraction` (`scale_row_count`'s floor applies per table).

    MASK-kind tables are untouched here -- their row count lives in
    resident source data (`downscale_sources`), not `config`, per this
    module's docstring. A table dict missing/None `row_count` (a mask
    table, or a malformed generate table the schema would have already
    rejected upstream) is left alone rather than guessed at.
    """
    scaled = copy.deepcopy(config)
    for table in scaled.get("tables") or []:
        if not isinstance(table, dict):
            continue
        if not table.get("generate_columns"):
            continue
        row_count = table.get("row_count")
        if isinstance(row_count, int):
            table["row_count"] = scale_row_count(row_count, fraction, floor_rows=floor_rows)
    return scaled


def downscale_sources(
    sources: dict[str, pa.Table], fraction: float, *, floor_rows: int = DEFAULT_PROBE_FLOOR_ROWS
) -> dict[str, pa.Table]:
    """Every resident table in `sources`, head-sliced to `scale_row_count`
    rows (same `fraction` + `floor_rows` for every table -- preserves each
    table's ROW-COUNT RATIO to the others, per this module's docstring; NOT
    a guarantee of referential/FK closure -- a sliced child row's FK key
    may point outside the sliced parent's key range. See this module's
    docstring for why that does not translate into an unsafe probe
    under-prediction.

    A head slice (`pa.Table.slice(0, n)`), not a random sample: deterministic
    (two probe runs of the same job at the same fraction measure the exact
    same rows, so any peak difference is real signal, not sampling noise),
    and zero-copy (an Arrow slice is a view, not a copy) so downscaling costs
    nothing beyond the write-to-Parquet `run_pipeline_isolated` already pays
    to hand the child its payload.
    """
    return {
        name: table.slice(0, scale_row_count(table.num_rows, fraction, floor_rows=floor_rows))
        for name, table in sources.items()
    }


@dataclass(frozen=True)
class ScaledJob:
    """One down-scaled job: a `config` + `sources` pair `run_pipeline_isolated`
    can execute, plus the ACTUAL (floor-adjusted) row count each table landed
    on -- `row_counts` is the ground truth for the probe's x-axis, since
    `scale_row_count`'s floor means the requested fraction and the achieved
    row count can differ from a naive `round(row_count * fraction)`.
    """

    config: dict[str, Any]
    sources: dict[str, pa.Table]
    row_counts: dict[str, int] = field(default_factory=dict)


def downscale_job(
    config: dict[str, Any],
    sources: dict[str, pa.Table] | None,
    fraction: float,
    *,
    floor_rows: int = DEFAULT_PROBE_FLOOR_ROWS,
) -> ScaledJob:
    """`downscale_config` + `downscale_sources` composed into one `ScaledJob`,
    `_probe.py`'s single entry point into this module.
    """
    scaled_config = downscale_config(config, fraction, floor_rows=floor_rows)
    scaled_sources = downscale_sources(sources or {}, fraction, floor_rows=floor_rows)
    row_counts: dict[str, int] = {name: table.num_rows for name, table in scaled_sources.items()}
    for table in scaled_config.get("tables") or []:
        if not isinstance(table, dict) or not table.get("generate_columns"):
            continue
        row_count = table.get("row_count")
        name = table.get("name")
        if isinstance(row_count, int) and isinstance(name, str):
            row_counts[name] = row_count
    return ScaledJob(config=scaled_config, sources=scaled_sources, row_counts=row_counts)

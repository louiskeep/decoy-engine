"""Route-local `pool_quality` obligation enforcement (Task 3.2).

`_capabilities.py`'s `_POOL_QUALITY = ("pool_quality",)` tags the `faker` and
`<composite>` entries with a class-A quality obligation but has zero
consumers today; this module is the FIRST. It does not touch
`capabilities_for` or the general capability resolver (HIGH 4
non-interference) -- it is a new, separate enforcement function that a
caller invokes only for the C1 native route.

There is no engine end-of-stream hook for the chunked native/oracle routes
(both are lazy generators), so `enforce_pool_quality` is STANDALONE: the
platform coordinator calls it before publication once it has a complete
`PoolQualityMeasurement` (deferred to Task 3.5, gated on Cam's
streaming-flip). This module is built and tested standalone here; it is NOT
wired into `run_native_or_oracle_chunked`'s streaming path.

Frozen metric definition, threshold formula, and bounded-aggregation SQL:
`docs/plans/PHASE3-C1-BASELINE.md`, "Frozen `pool_quality` metric" (Task 3.0
Step 4, committed BEFORE any measurement) and "Measured results (Step 5) +
oracle self-check (Step 6)" (the oracle-observed rates the thresholds below
are derived from). Threshold selection is per-column only (collision rate
and pool-duplicate rate are both tier-invariant for the frozen C1 recipe:
`pool_size` and each column's `distinct_sources` are fixed config knobs, not
functions of row count -- see the baseline doc's "both tiers" note).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.errors import DecoyError
from decoy_engine.execution.out_of_core._duckdb import connect_duckdb
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.generation.pool._value_pool import ValuePool

# UNIQUE-feasibility is N/A for every C1 column: JC-5 scopes Phase 3 to
# `reuse`-cardinality faker columns only, so the UNIQUE-feasibility check
# never applies. Recorded literally rather than silently passed (PHASE3-C1-
# BASELINE.md, "UNIQUE-feasibility").
UNIQUE_FEASIBILITY_NA = "N/A (reuse-only C1 scope)"

# The one numeric knob the Task 3.0 freeze commits to (PHASE3-C1-BASELINE.md
# line 237): threshold(column, metric) = oracle_observed_rate + MARGIN.
MARGIN = 0.02

# Oracle-observed rates, pinned run (`substrate="pandas"`, `execution_mode=
# "full_frame"`, `auto_chunk=False`), PHASE3-C1-BASELINE.md "pool_quality
# (both tiers)" table. Identical at the parity (10,000-row) and memory
# (3,000,000-row) tiers because pool_size (10,000) and each column's
# distinct_sources (1,000 FIRST / 1,200 LAST / 360 MAIDEN) are fixed by the
# frozen recipe, not derived from row count.
ORACLE_COLLISION_RATE: Mapping[str, float] = {
    "FIRST": 0.6430,
    "LAST": 0.5617,
    "MAIDEN": 0.2944,
}
ORACLE_POOL_DUPLICATE_RATE: Mapping[str, float] = {
    "FIRST": 0.9338,
    "LAST": 0.9013,
    "MAIDEN": 0.9017,
}

# threshold = oracle_observed_rate + MARGIN (PHASE3-C1-BASELINE.md "Frozen
# per-tier thresholds" table: FIRST 0.6630 / 0.9538, LAST 0.5817 / 0.9213,
# MAIDEN 0.3144 / 0.9217). Derived here, not hand-copied, so the provenance
# stays explicit; Step 6 of the baseline run asserts the oracle itself
# passes these (a non-negative margin over its own observed rate can never
# make the oracle fail its own bar).
COLLISION_RATE_THRESHOLD: Mapping[str, float] = {
    column: rate + MARGIN for column, rate in ORACLE_COLLISION_RATE.items()
}
POOL_DUPLICATE_RATE_THRESHOLD: Mapping[str, float] = {
    column: rate + MARGIN for column, rate in ORACLE_POOL_DUPLICATE_RATE.items()
}

# The frozen C1 obligation this module enforces. A caller passing any other
# obligation string is rejected coded rather than silently skipped (Task 3.2
# step 3).
_ENFORCED_OBLIGATION = "pool_quality"

# Bounded aggregation (PHASE3-C1-BASELINE.md "Bounded aggregation"): a
# spill-backed DuckDB `GROUP BY source` over a Parquet spool of the
# (source, masked) pairs. `non_deterministic_sources` is a QC check on the
# measurement itself (the deterministic sampler must never map one source to
# two outputs), not part of the pool_quality metric proper.
_COLLISION_SQL = """
WITH per_source AS (
    SELECT source, ANY_VALUE(masked) AS out_val,
           COUNT(DISTINCT masked) AS n_distinct_masked
    FROM read_parquet(?)
    WHERE source IS NOT NULL AND masked IS NOT NULL
    GROUP BY source
)
SELECT COUNT(*) AS distinct_sources,
       COUNT(DISTINCT out_val) AS distinct_outputs_for_distinct_sources,
       SUM(CASE WHEN n_distinct_masked > 1 THEN 1 ELSE 0 END) AS non_deterministic_sources
FROM per_source
"""


class PoolQualityError(DecoyError):
    """A `pool_quality` obligation was breached, or the obligation/column was
    outside the frozen C1 set, before publication.

    Fail-before-output, mirroring `CryptoExtensionUnavailableError`'s
    contract (`execution/native/_crypto_ext.py`): raised before the row ever
    reaches a sink. One stable `.code` for every raise site so a catcher can
    match programmatically; `.metric` disambiguates which check failed
    (`collision_rate`, `pool_duplicate_rate`, `non_deterministic_sources`, or
    `obligation` / `column` for an out-of-scope request). `.observed` and
    `.threshold` carry the values named in the message.
    """

    code: str = "pool_quality.threshold_exceeded"

    def __init__(
        self,
        message: str,
        *,
        column: str,
        metric: str,
        observed: Any,
        threshold: Any,
    ) -> None:
        self.column = column
        self.metric = metric
        self.observed = observed
        self.threshold = threshold
        super().__init__(message)


@dataclass(frozen=True)
class PoolQualityMeasurement:
    """One column's bounded `pool_quality` measurement.

    `unique_feasibility` is always `UNIQUE_FEASIBILITY_NA` for C1 (reuse-only
    scope); the field exists so a future UNIQUE-cardinality consumer has
    somewhere to record a real value instead of the check being silently
    absent.
    """

    column: str
    distinct_sources: int
    non_deterministic_sources: int
    collision_rate: float
    pool_size: int
    pool_duplicate_rate: float
    unique_feasibility: str = UNIQUE_FEASIBILITY_NA


def measure_pool_quality(
    *,
    column: str,
    source: pa.Array | pa.ChunkedArray,
    masked: pa.Array | pa.ChunkedArray,
    pool: ValuePool,
    temp_dir: Path,
    memory_limit: str | None = None,
) -> PoolQualityMeasurement:
    """Bounded `pool_quality` measurement for one admitted faker column.

    Collision measurement never materializes an `O(distinct sources)`
    Python-side structure (no `set()` over sources or outputs): the
    (source, masked) pairs are spooled to `pairs_<column>.parquet` and the
    frozen `GROUP BY source` aggregation runs inside a spill-backed DuckDB
    connection, so peak memory is bounded by the `memory_limit` config, not
    by distinct-source count. The connection is `connect_duckdb` from the
    out-of-core route (reused, not forked): it carries the memory-safety
    settings this measurement depends on, including the `threads`-vs-
    `memory_limit` clamp that keeps DuckDB's per-thread working set from
    blowing the budget on a many-core host, and the `0o700` temp-dir
    restriction. `pool_duplicate_rate` reads `pool.size` /
    `pool.distinct_count`, already `O(pool_size)` (`ValuePool` computes
    `distinct_count` at build time; see `generation/pool/_value_pool.py`),
    never a function of row count.

    `source` and `masked` must be the same length; a null `source` (or null
    `masked`) entry is excluded from the collision population by the frozen
    SQL's own `WHERE source IS NOT NULL AND masked IS NOT NULL` (never
    filtered in Python first, so the exclusion is auditable in one place).

    The pairs spool holds CLEARTEXT source values; it is deleted before this
    returns, and the temp dir is `0o700`-restricted by `connect_duckdb`. The
    caller must pass a secured, job-scoped `temp_dir`.
    """
    # Open the connection FIRST: connect_duckdb creates and `0o700`-restricts
    # temp_dir, so the cleartext-PII spool never lands in an unrestricted
    # directory (and if connect_duckdb itself fails, no spool is ever written).
    # Only then write the spool INTO the secured dir, and unlink it no matter
    # what -- the unlink is in the outer finally with close in an inner one, so
    # a query, write, or close failure can never leave cleartext PII at rest.
    pairs_path = temp_dir / f"pairs_{column}.parquet"
    conn = connect_duckdb(temp_dir=temp_dir, memory_limit=memory_limit)
    try:
        pq.write_table(pa.table({"source": source, "masked": masked}), pairs_path)
        row = conn.execute(_COLLISION_SQL, [str(pairs_path)]).fetchone()
    finally:
        try:
            pairs_path.unlink(missing_ok=True)
        finally:
            conn.close()

    distinct_sources = int(row[0])
    distinct_outputs = int(row[1])
    # SUM over zero input rows returns NULL, not 0 (unlike COUNT); an empty
    # population is a pass at rate 0.0 (below), so this only matters when
    # distinct_sources > 0 and no source repeated -- still NULL, still 0.
    non_deterministic_sources = int(row[2] or 0)

    if distinct_sources == 0:
        # Empty population (e.g. an all-null source column): rate 0, pass,
        # per the frozen metric definition -- recorded, never omitted.
        collision_rate = 0.0
    elif non_deterministic_sources != 0:
        # `out_val = ANY_VALUE(masked)` is arbitrary when a source maps to
        # more than one output, so a collision_rate computed over it is not
        # reproducible run-to-run. Report NaN rather than an unstable number;
        # enforcement raises on `non_deterministic_sources != 0` first, so
        # this value is never compared against a threshold on the enforce path.
        collision_rate = float("nan")
    else:
        collision_count = distinct_sources - distinct_outputs
        collision_rate = collision_count / distinct_sources

    pool_duplicate_count = pool.size - pool.distinct_count
    pool_duplicate_rate = pool_duplicate_count / pool.size if pool.size else 0.0

    return PoolQualityMeasurement(
        column=column,
        distinct_sources=distinct_sources,
        non_deterministic_sources=non_deterministic_sources,
        collision_rate=collision_rate,
        pool_size=pool.size,
        pool_duplicate_rate=pool_duplicate_rate,
    )


def _breach_message(
    column: str,
    metric: str,
    observed: float,
    threshold: float,
    warnings: Sequence[QualityWarning],
) -> str:
    message = (
        f"pool_quality breach on column {column!r}: {metric} {observed:.4f} "
        f"exceeds the frozen threshold {threshold:.4f}"
    )
    if warnings:
        codes = ", ".join(sorted({w.code for w in warnings}))
        message += f" (concurrent QualityWarning codes: {codes})"
    return message


def enforce_pool_quality(
    measurement: PoolQualityMeasurement,
    *,
    column: str,
    obligation: str = _ENFORCED_OBLIGATION,
    warnings: Sequence[QualityWarning] = (),
) -> None:
    """Route-local `pool_quality` gate for the native C1 faker route.

    Callers invoke this ONLY for a column the C1 phase3 eligibility
    predicate (Task 3.3) admitted; it has no opinion on eligibility itself
    and never runs for a non-C1 obligation or a non-C1 path (HIGH 4 -- this
    function does not read or change `capabilities_for` or the general
    resolver). Raises `PoolQualityError` before publication when:

    - `obligation` is anything other than `"pool_quality"` (an unrecognized
      obligation on this route is rejected, never silently passed);
    - `column` is outside the frozen C1 threshold set (`FIRST`, `LAST`,
      `MAIDEN`), for the same reason;
    - `measurement.non_deterministic_sources != 0` -- a measurement-integrity
      failure (the deterministic sampler must never map one source to two
      outputs), checked regardless of the numeric thresholds;
    - `measurement.collision_rate` or `measurement.pool_duplicate_rate`
      exceeds its frozen per-column threshold.

    A `QualityWarning` alone never raises; `warnings` is folded into the
    breach message only when a numeric threshold is actually exceeded, so a
    mere warning on a compliant pool is not escalated to a hard failure.
    """
    if obligation != _ENFORCED_OBLIGATION:
        raise PoolQualityError(
            f"unrecognized quality obligation {obligation!r} on the C1 native "
            f"faker route; only {_ENFORCED_OBLIGATION!r} is enforced here",
            column=column,
            metric="obligation",
            observed=obligation,
            threshold=_ENFORCED_OBLIGATION,
        )

    if column not in COLLISION_RATE_THRESHOLD:
        raise PoolQualityError(
            f"column {column!r} is not in the frozen C1 pool_quality column set "
            f"{sorted(COLLISION_RATE_THRESHOLD)}; refusing to silently pass an "
            "unrecognized column",
            column=column,
            metric="column",
            observed=column,
            threshold=sorted(COLLISION_RATE_THRESHOLD),
        )

    if measurement.column != column:
        raise PoolQualityError(
            f"measurement is for column {measurement.column!r} but enforcement was "
            f"requested for {column!r}; refusing to apply a mismatched column's "
            "threshold to a different column's measurement",
            column=column,
            metric="column",
            observed=measurement.column,
            threshold=column,
        )

    if measurement.non_deterministic_sources != 0:
        raise PoolQualityError(
            f"column {column!r}: {measurement.non_deterministic_sources} source "
            "value(s) mapped to more than one masked output; the deterministic "
            "sampler must never do this (measurement-integrity failure, not a "
            "tolerance breach)",
            column=column,
            metric="non_deterministic_sources",
            observed=measurement.non_deterministic_sources,
            threshold=0,
        )

    # A rate must be a finite value in [0, 1]. A NaN (e.g. the collision_rate
    # measure_pool_quality reports when non_deterministic_sources != 0, or any
    # value in a hand-built measurement) would fail OPEN under `>` comparison
    # (NaN > x is False), so reject it as a coded integrity failure BEFORE the
    # threshold checks. The non-determinism gate above already caught the
    # internal NaN path; this covers the standalone enforcer's accepted-input
    # contract.
    for metric_name, rate in (
        ("collision_rate", measurement.collision_rate),
        ("pool_duplicate_rate", measurement.pool_duplicate_rate),
    ):
        if not math.isfinite(rate) or not (0.0 <= rate <= 1.0):
            raise PoolQualityError(
                f"column {column!r}: {metric_name} is {rate!r}, not a finite rate in "
                "[0, 1]; refusing to compare a non-finite or out-of-range rate against "
                "a threshold (measurement-integrity failure)",
                column=column,
                metric=metric_name,
                observed=rate,
                threshold="[0.0, 1.0]",
            )

    collision_threshold = COLLISION_RATE_THRESHOLD[column]
    if measurement.collision_rate > collision_threshold:
        raise PoolQualityError(
            _breach_message(
                column, "collision_rate", measurement.collision_rate, collision_threshold, warnings
            ),
            column=column,
            metric="collision_rate",
            observed=measurement.collision_rate,
            threshold=collision_threshold,
        )

    duplicate_threshold = POOL_DUPLICATE_RATE_THRESHOLD[column]
    if measurement.pool_duplicate_rate > duplicate_threshold:
        raise PoolQualityError(
            _breach_message(
                column,
                "pool_duplicate_rate",
                measurement.pool_duplicate_rate,
                duplicate_threshold,
                warnings,
            ),
            column=column,
            metric="pool_duplicate_rate",
            observed=measurement.pool_duplicate_rate,
            threshold=duplicate_threshold,
        )


__all__ = [
    "COLLISION_RATE_THRESHOLD",
    "MARGIN",
    "ORACLE_COLLISION_RATE",
    "ORACLE_POOL_DUPLICATE_RATE",
    "POOL_DUPLICATE_RATE_THRESHOLD",
    "UNIQUE_FEASIBILITY_NA",
    "PoolQualityError",
    "PoolQualityMeasurement",
    "enforce_pool_quality",
    "measure_pool_quality",
]

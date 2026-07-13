"""Pure, schema-derived peak-memory estimator (OOM-avoidance routing redesign,
Sprint B1a -- docs/plans/2026-07-10-oom-avoidance-routing-redesign.md §3.2/§3.3/
§3.6, corrected per §11; k-constant premise corrected again per the dennis BLOCK
on the initial B1a pass, 2026-07-11 -- see `K_FULL_FRAME_SLOPE` below).

This module computes bytes; it does not route. `decide_execution_route`
(`_pipeline_routing.py`) and the reject constants are untouched here --
wiring this estimator into routing is Sprint B1b. No pandas/DuckDB/platform
imports: the arithmetic is plain Python over small dataclasses, so it is
callable from a router, a CLI dry-run, or a test with zero execution cost.

Two-factor model (§3.2), kept as two separate terms because collapsing them
into one row-count constant is exactly what made the prior design
(`OUT_OF_CORE_THRESHOLD_ROWS_DEFAULT` / `FULL_FRAME_REJECT_ROWS_DEFAULT`)
mis-route on any schema unlike its one calibration shape:

  1. `raw_data_bytes` -- schema-derived, computed (never calibrated): per
     table, rows times the sum of each column's byte cost. Fixed-width
     dtypes (int64, float64, datetime64, bool, ...) have a known constant
     itemsize (this is just `numpy.dtype.itemsize`; pandas'
     `DataFrame.memory_usage()` reports the identical number for these
     dtypes without `deep=True`). Variable-width (object/string) columns
     need a per-cell byte cost that "shallow" memory_usage does NOT
     capture -- that gap is exactly why `deep=True` exists on
     `memory_usage()`, and why this module never treats a string column as
     "free": see `_STR_OBJECT_OVERHEAD_BYTES` below. IMPORTANT: this
     per-cell pricing is itself a worst-case assumption, not a measurement
     of what any given run will actually hold resident -- see point 2.

  2. `k_path` -- a per-execution-path multiplier meant to translate
     `raw_data_bytes` into a peak-RSS prediction. It is NOT schema-invariant,
     and it CANNOT be made schema-invariant from a single calibration run:
     `raw_data_bytes` prices every string cell independently (pointer +
     object header + payload, see `_STR_OBJECT_OVERHEAD_BYTES`), but real
     interpreters/columnar engines share memory for repeated string values
     (pooling/interning/dictionary-encoding) whenever cardinality is low
     relative to row count. A fixture built on a small shared string pool
     (the B1 calibration schema below) therefore has a raw_bytes that is
     inflated relative to its OWN true peak by however much pooling actually
     saved it -- and a `k = true_peak / raw_bytes` derived from that fixture
     silently bakes the fixture's OWN pooling ratio into a number this
     module then applies to every other schema, including ones with no
     pooling at all (numeric columns, high-cardinality/unique strings). A
     byte-level static description cannot know a future run's value
     cardinality, so there is no schema-invariant k this module can compute
     -- only a conservative one. See `K_FULL_FRAME_SLOPE`'s docstring
     for the concrete numbers and the resulting operational constant.

  B1b PRECONDITION (binding on whoever wires this into routing): because
  `k_path` is a coarse, conservative filter and not a precise predictor,
  B1b must NEVER statically route a job onto `full_frame` on the strength
  of a bare `estimate_peak_bytes(..., "full_frame")` call alone unless that
  conservative estimate clears the configured budget with the full
  asymmetric margin (`fits`'s `error_band`). Any estimate near the
  boundary -- or for a schema shape the static estimator cannot confidently
  bound (unpriceable columns, or priceable columns whose true cardinality
  is unknown) -- MUST be routed to the probe (B2) or a bounded path instead
  of trusted for full_frame admission. The probe and B5 telemetry measure
  the real per-schema peak; this module only filters out the clear cases.

Sequential is modeled separately (`estimate_peak_bytes` path="sequential"):
it is O(cardinality), and a `raw_bytes * k` model is structurally wrong for
it (§11 §3.2a) -- see that function's docstring.

This module holds only the pure arithmetic over the normalized
`TableSizeSpec`/`ColumnSizeSpec` shape. Building that shape from the
engine's real schema representations -- a mask table's `TableProfile`
(sampling its resident string columns; generation jobs have no input to
sample, §3.2b/§11) or a generate table's `TableConfig` (pricing string
columns from provider/strategy metadata instead) -- lives in the sibling
`_mem_estimate_schema` module, split out to keep this one under the
600-LOC orchestration cap (CLAUDE.md "Engineering best practices").
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

ExecutionPath = Literal["full_frame", "out_of_core", "sequential"]

# ---------------------------------------------------------------------------
# Fixed-width dtype cost table
# ---------------------------------------------------------------------------

# itemsize in bytes for every fixed-width dtype label `canonical_dtype_label`
# (internal/pandas_compat.py) can produce for a resident pandas Series, plus
# the plain numpy spellings a caller may pass directly. These are exactly
# `numpy.dtype(<label>).itemsize` -- not calibrated, just read off the numpy
# dtype table -- so a schema with more/fewer/different fixed-width columns
# is priced correctly without touching this module.
_FIXED_WIDTH_DTYPE_BYTES: dict[str, int] = {
    "int64": 8,
    "uint64": 8,
    "float64": 8,
    "datetime64[ns]": 8,
    "datetime64": 8,
    "timedelta64[ns]": 8,
    "timedelta64": 8,
    "int32": 4,
    "uint32": 4,
    "float32": 4,
    "int16": 2,
    "uint16": 2,
    "int8": 1,
    "uint8": 1,
    "bool": 1,
}

# dtype labels that are variable-width and therefore need a per-cell string
# cost rather than a table lookup. `canonical_dtype_label` emits "object" for
# both a masked source's inferred-string columns and pandas-3's `str` dtype
# (see its docstring); "string"/"string[pyarrow]" are the explicit pandas
# extension-dtype spellings. Anything not in this set and not in
# `_FIXED_WIDTH_DTYPE_BYTES` is an unrecognized dtype label -- `_column_bytes`
# fails closed on it rather than silently pricing it as free.
_VARIABLE_WIDTH_DTYPES = frozenset({"object", "string", "string[pyarrow]", "large_string"})


def is_fixed_width_dtype(dtype: str) -> bool:
    """Whether `dtype` prices as a fixed-width column (`_FIXED_WIDTH_DTYPE_BYTES`).

    Public so `_mem_estimate_schema`'s adapters can classify a profiled
    column's dtype without importing this module's private cost table
    directly.
    """
    return dtype in _FIXED_WIDTH_DTYPE_BYTES


# CPython's compact-ASCII str object carries a fixed 49-byte header
# regardless of length (measured: `sys.getsizeof("")` is 49 on CPython
# 3.10-3.13 x86-64; PEP 393's flexible string representation keeps the
# per-object header constant and adds 1 byte/char for the common
# latin1-storage case). The numpy/pandas "object" ndarray that holds a
# string stores an 8-byte PyObject* reference per cell on TOP of that
# (numpy's object-array docs: elements are references, not inline data),
# and THAT reference is never shared -- every cell needs its own slot in
# the array regardless of what it points to. The 49-byte header + payload,
# however, is NOT necessarily per-cell: when the SAME string object is
# reused across cells (pooling/interning, or a columnar engine's
# dictionary encoding for a low-cardinality column), that header and its
# character bytes are paid ONCE for the whole column, and every other cell
# just holds another 8-byte pointer to it. This module cannot know at
# estimate time whether a given column's runtime values will end up pooled
# -- that depends on value cardinality and on the specific execution
# path's copy behavior, neither of which a static schema description
# carries -- so it prices the full per-cell cost (pointer + header +
# payload) as a conservative default rather than assuming pooling that may
# not happen. This is precisely the accounting gap pandas' own
# `DataFrame.memory_usage(deep=True)` flag exists to close: the shallow
# (default) count only sees the 8-byte pointers and silently drops the
# string payload. See `K_FULL_FRAME_SLOPE`'s docstring for how this
# per-cell pricing interacts with the k-constant when a column IS pooled.
_STR_OBJECT_POINTER_BYTES = 8
_STR_OBJECT_HEADER_BYTES = 49
_STR_OBJECT_OVERHEAD_BYTES = _STR_OBJECT_POINTER_BYTES + _STR_OBJECT_HEADER_BYTES  # 57


# ---------------------------------------------------------------------------
# Column / table size specs -- the normalized input every function below
# operates on, regardless of whether it came from a profiled mask table or a
# generate table's config (see the `table_size_spec_from_*` adapters).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ColumnSizeSpec:
    """One column's size-relevant shape.

    `dtype` is a fixed-width label from `_FIXED_WIDTH_DTYPE_BYTES` or a
    variable-width label from `_VARIABLE_WIDTH_DTYPES`. For a variable-width
    column, exactly one of `string_width_bytes` (a declared/sampled/
    provider-derived average length in bytes) or `unpriceable=True` must be
    set: the estimator never falls back to a default average length,
    because a wrong guess there is exactly the "coefficients per schema"
    failure mode this redesign exists to avoid (§3.5).
    """

    name: str
    dtype: str
    string_width_bytes: float | None = None
    unpriceable: bool = False

    def __post_init__(self) -> None:
        if self.unpriceable and self.string_width_bytes is not None:
            raise ValueError(
                f"column {self.name!r}: unpriceable=True and string_width_bytes are "
                "mutually exclusive -- an unpriceable column has no width estimate."
            )
        is_fixed_width = self.dtype in _FIXED_WIDTH_DTYPE_BYTES
        is_variable_width = self.dtype in _VARIABLE_WIDTH_DTYPES
        if not is_fixed_width and not is_variable_width:
            raise ValueError(
                f"column {self.name!r}: unrecognized dtype {self.dtype!r}. Add it to "
                "_FIXED_WIDTH_DTYPE_BYTES (fixed-width) or _VARIABLE_WIDTH_DTYPES "
                "(variable-width) in _mem_estimate.py rather than pricing it silently."
            )
        if is_fixed_width and self.string_width_bytes is not None:
            raise ValueError(
                f"column {self.name!r}: dtype {self.dtype!r} is fixed-width; "
                "string_width_bytes only applies to a variable-width dtype."
            )
        if is_variable_width and not self.unpriceable and self.string_width_bytes is None:
            raise ValueError(
                f"column {self.name!r}: dtype {self.dtype!r} is variable-width and "
                "needs string_width_bytes (declared/sampled/provider-derived) or "
                "unpriceable=True."
            )


@dataclass(frozen=True)
class TableSizeSpec:
    """One table's size-relevant shape: a name, a row count, its columns."""

    name: str
    row_count: int
    columns: tuple[ColumnSizeSpec, ...]

    def __post_init__(self) -> None:
        if self.row_count < 0:
            raise ValueError(f"table {self.name!r}: row_count must be >= 0, got {self.row_count}.")


UnpriceableColumn = tuple[str, str]  # (table_name, column_name)


@dataclass(frozen=True)
class RawBytesResult:
    """The result of `raw_data_bytes`.

    `priceable_bytes` sums every column the estimator could price, even when
    `unpriceable_columns` is non-empty -- it is a partial figure (useful for
    telemetry/visibility), never a routing verdict on its own.
    `unpriceable_columns` names every `(table, column)` the estimator
    declined to guess at. A non-empty tuple means the caller must treat the
    whole estimate as UNKNOWN (§3.5: unpriceable -> probe/bounded path), not
    silently proceed on the partial sum.
    """

    priceable_bytes: int
    unpriceable_columns: tuple[UnpriceableColumn, ...] = ()

    @property
    def is_priceable(self) -> bool:
        return not self.unpriceable_columns


def _column_bytes(row_count: int, column: ColumnSizeSpec) -> int | None:
    """Bytes for one column across `row_count` rows, or `None` if unpriceable."""
    if column.unpriceable:
        return None
    fixed = _FIXED_WIDTH_DTYPE_BYTES.get(column.dtype)
    if fixed is not None:
        return row_count * fixed
    # Variable-width: `__post_init__` guarantees string_width_bytes is set here.
    width = column.string_width_bytes
    if width is None:
        raise AssertionError(
            f"column {column.name!r}: variable-width with no string_width_bytes; "
            "ColumnSizeSpec.__post_init__ should have rejected this construction."
        )
    per_cell = width + _STR_OBJECT_OVERHEAD_BYTES
    return int(row_count * per_cell)


def raw_data_bytes(tables: Sequence[TableSizeSpec]) -> RawBytesResult:
    """Sum of `rows * per-column byte cost` across every table (§3.2 factor 1).

    Computed, never calibrated: this is what absorbs width, table count, and
    dtype mix -- the exact dimensions a fixed row-count threshold cannot
    see. Any unpriceable column is named in the result rather than silently
    skipped or zero-priced.
    """
    total = 0
    unpriceable: list[UnpriceableColumn] = []
    for table in tables:
        for column in table.columns:
            priced = _column_bytes(table.row_count, column)
            if priced is None:
                unpriceable.append((table.name, column.name))
                continue
            total += priced
    return RawBytesResult(priceable_bytes=total, unpriceable_columns=tuple(unpriceable))


# ---------------------------------------------------------------------------
# Peak-memory model -- MEASURED intercept + per-route slope.
# History: cold-start guesses (2026-07-10) -> k-premise corrected (2026-07-11,
# dennis BLOCK) -> MEASURED two-point slopes pinned (TB-4, 2026-07-13) -> fixed
# INTERCEPT term added (TB-5 precondition, issue #72, 2026-07-13).
# ---------------------------------------------------------------------------
#
# --- The model: predicted = intercept + basis * slope ------------------------
#
# TB-4 (`scripts/tb4_calibration.py`; results in `docs/plans/
# 2026-07-13-tb4-calibration-results.md`) measured each route at TWO row scales
# under process isolation (`run_pipeline_isolated`). Peak is LINEAR in basis, so
# both terms fall out of the two points: slope = dpeak/dbasis, intercept =
# peak - slope*basis. The SLOPE is the per-byte cost; the INTERCEPT is the fixed
# interpreter/pyarrow/DuckDB baseline RSS a job pays before any data. TB-4
# pinned the slope but modeled THROUGH-ORIGIN (`basis * slope`), OMITTING the
# intercept, so it UNDER-predicts small-basis jobs -- e.g. numeric_fk full_frame
# @500k predicted 767 MB (191.7 MB basis * 4.0) vs a real 858 MB peak, -11%, the
# OOM-unsafe direction. This module now adds the intercept back.
#
# Fit from the numeric_fk shape (no string pooling -> the WORST-case per-byte
# slope a conservative constant must cover; `raw_data_bytes` prices it exactly):
#
#   route        | slope | measured intercept | pinned intercept
#   full_frame   | 3.45  |  197 MB            |  200 MiB (in-core, shared)
#   out_of_core  | 0.95  |  447 MB            |  450 MiB (DuckDB + budget)
#   sequential   | 3.28  |  172 MB            |  200 MiB (in-core, shared)
#
# The intercept is pinned PER ROUTE because it differs MATERIALLY: out_of_core
# (~2.3x) runs DuckDB and holds a budget-bounded buffer, not just the
# interpreter/pyarrow floor the in-core routes (full_frame, sequential) share.
# One shared 200 MB would UNDER-predict out_of_core (unsafe); one shared 450 MB
# would over-inflate in-core small jobs by ~250 MB. Each pinned intercept is the
# measured value rounded UP (§13: over-predict, never under). The SLOPES are the
# TB-4 values kept UNCHANGED -- each already exceeds its measured two-point slope
# (4.0 > 3.45, 1.5 > 0.95, 4.0 > 3.28) -- so with a strictly positive intercept
# `intercept + basis*slope` is >= the old `basis*slope` at EVERY size: it raises
# the omitted small-basis floor without lowering any prediction.

# K_CALIBRATION_ERROR_BAND: the tolerance the pinned constants sit within,
# ABOVE the measured max slope, for the routes the `basis * k` model prices
# tightly (full_frame 4.0/3.45 = +16%, sequential 4.0/3.28 = +22%). Covers
# TB-4's run-to-run variance (a few %, matching TB-3's ~±90 MB) plus headroom
# for unsampled schema shapes. `out_of_core` sits further above its slope
# (1.5/0.95 = +58%) on purpose -- its large fixed intercept makes a
# through-origin k structurally loose, and over-predicting the RAM-CAPPED
# fallback is the safe direction (see `K_OUT_OF_CORE_SLOPE`).
K_CALIBRATION_ERROR_BAND = 0.30

# RECALIBRATION TRIGGER (when to re-run `scripts/tb4_calibration.py` and
# re-pin these constants; §13 / doc B5):
#   1. DRIFT: B5 telemetry (`_mem_telemetry.recalibrate_k`) sees an isolated
#      job whose observed_k for a route exceeds current_k -> RAISE immediately
#      (safety), or the max observed_k over >= `min_samples_for_lower`
#      isolated jobs falls below current_k / (1 + K_CALIBRATION_ERROR_BAND)
#      -> consider LOWERING (gated). recalibrate_k enforces both directions.
#   2. DEPENDENCY/ROUTE CHANGE: a pyarrow / DuckDB / pandas major bump, or an
#      engine change to a route's buffering/copy behavior, invalidates the
#      measured slope+intercept -> re-run the sweep.
#   3. NEW SCHEMA CLASS: a production shape materially unlike the sampled
#      pooled/numeric/unique classes (e.g. wide-binary, deeply nested) ->
#      measure it before trusting a bare estimate for full_frame admission.

# K_FULL_FRAME_MEASURED_POOLED: EVIDENCE ONLY, kept for provenance/telemetry
# comparison -- the B1 pooled-string full_frame sweep (parent/child/
# grandchild FK, 16 pooled payload cols/table, `_string_pool(width=12)`)
# measured peak_RSS / raw_data_bytes converging to ~1.156 as rows grow
# (4,448 MB @ 1M rows -> 24,768 MB @ 6M; see the calibration test for the
# closed-form reproduction). Because that fixture pools cells from only 4096
# distinct strings, `raw_data_bytes` over-prices it ~8.5x, so 1.156 is a
# pooled-shape ARTIFACT, NOT schema-invariant (dennis BLOCK). TB-4's
# intercept-free pooled full_frame slope (2.11) is higher than 1.156 because
# it prices against a leaner 4-column pooled fixture; both are far below the
# numeric worst case. Never read this constant for a routing decision;
# `_K_PATH` does not reference it.
K_FULL_FRAME_MEASURED_POOLED = 1.156

# K_INTERCEPT_BYTES: the fixed baseline-RSS intercept for the IN-CORE routes
# (full_frame, sequential) -- the interpreter + pyarrow + pandas resident floor
# a job pays before its data-proportional (basis * slope) cost. MEASURED (TB-4
# two-point fit): ~197 MB full_frame, ~172 MB sequential; pinned at 200 MiB, the
# conservative max rounded up. Adding it closes the small-basis under-prediction
# a through-origin `basis * slope` left open (numeric_fk full_frame @500k: 767 ->
# 976 MB predicted, now >= the 858 MB real peak).
K_INTERCEPT_BYTES = 200 * 1024 * 1024

# K_OUT_OF_CORE_INTERCEPT_BYTES: out_of_core's larger fixed floor -- it runs
# DuckDB and holds a budget-bounded working buffer on top of the interpreter
# baseline, so its MEASURED intercept (~447 MB, TB-4) is ~2.3x the in-core
# routes'. Pinned PER ROUTE at 450 MiB (measured rounded up). Over-predicting
# the RAM-capped fallback is the safe direction (the runtime budget + governor,
# TB-1/TB-2/TB-3, remain out_of_core's real bound, not this estimate).
K_OUT_OF_CORE_INTERCEPT_BYTES = 450 * 1024 * 1024

# The per-byte SLOPES, applied ON TOP OF the route's intercept (peak =
# intercept + basis * slope). Each is the TB-4 max two-point slope (numeric FK,
# the no-pooling worst case) rounded UP; the round-up bounds per-byte GROWTH for
# every OOM-relevant job while staying above the pooled/unique shapes it over-
# prices (the safe direction). Renamed from the TB-4 `K_*_COLD_START` combined
# multipliers when the fixed intercept was split out: PURE slopes now.
K_FULL_FRAME_SLOPE = 4.0  # measured 3.45 (+16%, within K_CALIBRATION_ERROR_BAND)
# out_of_core is RAM-CAPPED (`out_of_core/_budget.py`): peak grows SUB-linearly
# (slope < 1.0, chunks budget-bounded). 1.5 stays > 1.0 (LESS likely to claim it
# fits); K_OUT_OF_CORE_INTERCEPT_BYTES now covers its large fixed floor.
K_OUT_OF_CORE_SLOPE = 1.5  # measured 0.95
K_SEQUENTIAL_SLOPE = 4.0  # measured 3.28 (+22%); in-core floor like full_frame

_K_PATH: dict[ExecutionPath, float] = {
    "full_frame": K_FULL_FRAME_SLOPE,
    "out_of_core": K_OUT_OF_CORE_SLOPE,
    "sequential": K_SEQUENTIAL_SLOPE,
}

# Per-route fixed intercept (baseline RSS): the in-core routes share the
# interpreter/pyarrow floor; out_of_core carries its larger DuckDB + budget floor.
_K_INTERCEPT_BYTES: dict[ExecutionPath, int] = {
    "full_frame": K_INTERCEPT_BYTES,
    "out_of_core": K_OUT_OF_CORE_INTERCEPT_BYTES,
    "sequential": K_INTERCEPT_BYTES,
}

# CPython's compact dict/set keeps a dense entry table (hash + key pointer +
# value pointer -- 24 bytes/entry on 64-bit builds, `Objects/dictobject.c`)
# behind a sparse index sized so the table never exceeds ~2/3 load factor;
# at that load factor the sparse index adds roughly another 8-16 bytes/entry
# amortized. `_HASH_ENTRY_OVERHEAD_BYTES` folds both into one flat
# per-key allowance ON TOP OF the key object's own bytes.
_HASH_ENTRY_OVERHEAD_BYTES = 32


def default_fk_key_size_bytes(key_width_bytes: float) -> int:
    """Working-set bytes for one FK key held in a dedup/lookup table.

    A key's own object cost (the same string-object model `raw_data_bytes`
    uses: pointer + CPython header + character bytes) plus
    `_HASH_ENTRY_OVERHEAD_BYTES` for the hash-table slot that indexes it.
    Exposed so a caller (B1b) can build a `FkCardinalityInput` from a real
    key-width sample without re-deriving the hash-table overhead constant.
    """
    return int(key_width_bytes + _STR_OBJECT_OVERHEAD_BYTES + _HASH_ENTRY_OVERHEAD_BYTES)


@dataclass(frozen=True)
class FkCardinalityInput:
    """Sequential-path sizing input (§11 §3.2a): sequential is O(cardinality),
    not O(raw_bytes) -- `run_sequential` streams/evicts table by table.

    `working_set_bytes` (computed by `estimate_peak_bytes`'s "sequential"
    branch from `tables` itself, not by the caller) is currently the SUM of
    the two largest tables' raw bytes, not a single table's -- an RI join
    can hold the parent's key set resident while it streams the child table
    that references it, so two tables concurrently resident is the safe
    (conservative) assumption pending verification of the tighter
    single-table bound. That tightening is gated on PR #22 (`run_sequential`
    rework, §3.2a) landing and its actual concurrent-table behavior being
    measured -- do not narrow this back to a single table before then.
    `distinct_key_count` and `key_size_bytes` price the FK dedup table on
    top of that: `distinct_key_count * key_size_bytes` bytes, scaling with
    key CARDINALITY, never with row count directly -- a wide table with a
    low-cardinality FK costs the same here regardless of how many rows
    share each key.
    """

    distinct_key_count: int
    key_size_bytes: int

    def __post_init__(self) -> None:
        if self.distinct_key_count < 0:
            raise ValueError("distinct_key_count must be >= 0.")
        if self.key_size_bytes < 0:
            raise ValueError("key_size_bytes must be >= 0.")


@dataclass(frozen=True)
class PeakEstimate:
    """The result of `estimate_peak_bytes`: a priced byte figure, or the
    UNPRICEABLE marker. A caller (the future router) MUST treat
    `unpriceable=True` as "route to a probe/bounded path" (§3.5) -- never
    silently fall back to 0 or another default.
    """

    estimated_bytes: int | None
    unpriceable_columns: tuple[UnpriceableColumn, ...] = ()

    def __post_init__(self) -> None:
        if self.estimated_bytes is None and not self.unpriceable_columns:
            raise ValueError(
                "PeakEstimate: estimated_bytes=None requires at least one "
                "unpriceable_columns entry explaining why."
            )

    @property
    def unpriceable(self) -> bool:
        return self.estimated_bytes is None


def estimate_peak_bytes(
    tables: Sequence[TableSizeSpec],
    path: ExecutionPath,
    *,
    fk_cardinality: FkCardinalityInput | None = None,
) -> PeakEstimate:
    """Predict peak resident bytes for `tables` on `path` (§3.2/§3.3).

    full_frame / out_of_core: `intercept + raw_data_bytes(tables) * slope` --
    a fixed baseline-RSS intercept plus the per-byte term. sequential: the
    same intercept plus a cardinality term, NOT `raw_bytes *
    k_seq` (§11 §3.2a: sequential streams table-by-table, so its cost is
    bounded by a SMALL, FIXED number of concurrently-resident tables plus
    FK dedup working set, not the SUM of every table's bytes -- summing
    every table would make it scale with total row volume across every
    table, which is exactly the O(raw_bytes) model the plan says is
    structurally wrong for this path).

    The working set is conservatively modeled as the SUM of the two
    LARGEST tables' raw bytes (an RI join can hold a parent's key set
    resident while streaming its child), not a single table's -- see
    `FkCardinalityInput`'s docstring for why, and for the PR #22 gate on
    tightening this to a single table.
    """
    if path in ("full_frame", "out_of_core"):
        raw = raw_data_bytes(tables)
        if not raw.is_priceable:
            return PeakEstimate(estimated_bytes=None, unpriceable_columns=raw.unpriceable_columns)
        predicted = _K_INTERCEPT_BYTES[path] + raw.priceable_bytes * _K_PATH[path]
        return PeakEstimate(estimated_bytes=int(predicted))

    if path == "sequential":
        per_table_raw = [raw_data_bytes((table,)) for table in tables]
        unpriceable = tuple(c for r in per_table_raw for c in r.unpriceable_columns)
        if unpriceable:
            return PeakEstimate(estimated_bytes=None, unpriceable_columns=unpriceable)
        # Sum of the two largest tables, not `max()` of one: conservative
        # cover for an RI join holding parent-keys + child concurrently.
        # Narrowing this to a single table is gated on PR #22 (`run_sequential`
        # rework) landing and its concurrency behavior being verified.
        largest_two = sorted((r.priceable_bytes for r in per_table_raw), reverse=True)[:2]
        working_set_bytes = sum(largest_two)
        fk_bytes = 0
        if fk_cardinality is not None:
            fk_bytes = fk_cardinality.distinct_key_count * fk_cardinality.key_size_bytes
        return PeakEstimate(
            estimated_bytes=int(
                K_INTERCEPT_BYTES + (working_set_bytes + fk_bytes) * K_SEQUENTIAL_SLOPE
            )
        )

    raise ValueError(f"unknown execution path {path!r}")


def fits(
    tables: Sequence[TableSizeSpec],
    path: ExecutionPath,
    budget_bytes: int,
    *,
    error_band: float = K_CALIBRATION_ERROR_BAND,
    fk_cardinality: FkCardinalityInput | None = None,
) -> bool | None:
    """Whether `path` fits `budget_bytes` with the asymmetric margin (§3.6).

    Returns `estimate * (1 + error_band) < budget_bytes`. `error_band`
    starts fat (~30%, the default) and is meant to narrow only as B5
    telemetry tightens the estimator -- the margin is applied on the
    "claims it fits" side only (never loosened the other way), because a
    false "fits" risks an OOM kill while a false "doesn't fit" only costs a
    downgrade to a slower, still-correct bounded path (§3.6's wall-clock-
    vs-total-loss asymmetry).

    Returns `None` -- not `False` -- when the estimate is UNPRICEABLE:
    "does not fit" is a specific claim this function has no basis to make,
    and coercing an unknown into a fixed boolean would silently reintroduce
    the guessing §3.5 exists to prevent. A caller must handle `None`
    explicitly (route to the probe/bounded path), not treat it as `False`.
    """
    if error_band < 0:
        raise ValueError(f"error_band must be >= 0, got {error_band}.")
    if budget_bytes <= 0:
        raise ValueError(f"budget_bytes must be positive, got {budget_bytes}.")
    estimate = estimate_peak_bytes(tables, path, fk_cardinality=fk_cardinality)
    if estimate.unpriceable:
        return None
    estimated_bytes = estimate.estimated_bytes
    if estimated_bytes is None:
        raise AssertionError(
            "PeakEstimate.unpriceable is False but estimated_bytes is None; "
            "PeakEstimate.__post_init__ should have rejected this construction."
        )
    return estimated_bytes * (1 + error_band) < budget_bytes


__all__ = [
    "K_CALIBRATION_ERROR_BAND",
    "K_FULL_FRAME_MEASURED_POOLED",
    "K_FULL_FRAME_SLOPE",
    "K_INTERCEPT_BYTES",
    "K_OUT_OF_CORE_INTERCEPT_BYTES",
    "K_OUT_OF_CORE_SLOPE",
    "K_SEQUENTIAL_SLOPE",
    "ColumnSizeSpec",
    "ExecutionPath",
    "FkCardinalityInput",
    "PeakEstimate",
    "RawBytesResult",
    "TableSizeSpec",
    "default_fk_key_size_bytes",
    "estimate_peak_bytes",
    "fits",
    "is_fixed_width_dtype",
    "raw_data_bytes",
]

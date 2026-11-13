"""Canonical FK match keys, and the lossless FK-integer-typing contract.

The match-key helpers (`fk_key_value`, `fk_join_key*`) define equality for
relationship joins. They are deliberately separate from derive-input
canonicalization, whose output bytes are part of the seed protocol and reject
floats.

The dtype helpers below (`FK_KEY_DTYPE_UNSUPPORTED_CODE`,
`lossless_fk_int_values`, `fk_nullable_int_array`, `fk_columns_for_table`,
`to_pandas_fk_safe`) are the DE-10 lossless-typing contract: the ONE place
that decides whether an FK integer key survives a pandas round trip exactly
or the run fails closed, shared by the full-frame, sequential, and chunked
routes (all three run through `PandasExecutionAdapter`) and referenced by the
out-of-core route's existing `out_of_core_fk_key_dtype_unsupported` rejection
so every route raises the identical code for the identical shape (see module
docstring section "Root cause" in this file's git history / DE-10 report).

Root cause: pandas has no lossless way to store an integer column that
contains a null next to numpy's fixed-width integer types -- `to_pandas()`
and a raw Python-list column assignment both fall back to float64 (NaN for
the null), and float64 cannot exactly hold an integer beyond 2**53. The
out-of-core route never touches pandas for its relational data (Arrow arrays
ride through as-is; see `out_of_core/_relation.py::_cast_masked_array`), so it
does not have this failure mode by construction, and already fails closed
(`out_of_core_fk_key_dtype_unsupported`) on the one shape it truly cannot
resolve (a fractional value sharing a column with an integer beyond exact
float precision). This contract brings the pandas-backed routes to the same
standard: exact when a lossless representation exists, and the SAME typed
error when it does not.

The lossless representation is EACH integer type's OWN nullable pandas
extension dtype (`Int8`/`Int16`/`Int32`/`Int64`/`UInt8`/`UInt16`/`UInt32`/
`UInt64`), not a single blanket signed `Int64` cast. An unconditional Int64
cast (the DE-10 rework's initial cut) has two failure modes of its own: it
WIDENS a narrower key (e.g. a source `int32` auto-increment PK) to `int64` in
the output, changing the output schema for a job that never needed lossless
typing; and it cannot hold an unsigned key in `[2**63, 2**64)` (unsigned
snowflake/bigserial IDs) at all, raising an uncoded `pyarrow.lib.ArrowInvalid`
on the pandas routes while the out-of-core route raised the coded rejection
for the equivalent shape -- itself a route-dependent divergence of the exact
kind this contract exists to close. Mapping each Arrow integer type to its
own same-width, same-signedness pandas dtype preserves width (no widening)
and preserves the full unsigned range (no unsigned overflow), so this
contract now covers signed, unsigned, and narrower-than-int64 FK keys, not
"int64 only".
"""

from __future__ import annotations

import numbers
from collections.abc import Collection, Iterable, Sequence
from typing import TYPE_CHECKING, Final, NoReturn

import pandas as pd
import pyarrow as pa

from decoy_engine import reasons
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.internal.fk_tokens import (  # noqa: F401  re-exported for every FK route
    NULL_FK_KEY,
    _decimal_join_token,
    fk_join_key,
    fk_join_key_tuple,
    fk_key_value,
)
from decoy_engine.internal.pandas_compat import (
    exact_int_types_mapper,
    table_to_pandas_exact_ints,
)

if TYPE_CHECKING:
    from decoy_engine.relationships._graph import RelationshipEdge

# The SAME stable code every route raises for "no dtype can hold this FK
# output column losslessly": originally minted by the out-of-core route
# (`out_of_core/_join.py::cast_fk_chunk` / `_append_output_batch`,
# `out_of_core/_batch_join.py`) for the one shape it cannot resolve from
# Arrow types alone. DE-10 reuses the identical string (not a new code) so a
# caller branching on `ExecutionError.code` sees one contract regardless of
# which route ran the job; the name keeps its historical `out_of_core_`
# prefix to avoid rewriting the out-of-core route's own pinned tests/docs
# (tests/parity/SEMANTIC_DIFFERENCES.md, test_out_of_core_fk_parity.py) for a
# cosmetic rename.
FK_KEY_DTYPE_UNSUPPORTED_CODE: Final = reasons.runtime.OUT_OF_CORE_FK_KEY_DTYPE_UNSUPPORTED

# pyarrow's safe cast rejects (and this module's own round-trip check treats
# as lossy) any int64 -> float64 conversion for a magnitude outside this
# range, even a value that happens to be exactly representable -- IEEE 754
# double precision has a 53-bit mantissa, so 2**53 is the largest magnitude
# every integer up to and including it can round-trip through float64 exactly.
_EXACT_FLOAT_INT_BOUND: Final = 2**53


def _is_exactly_float_representable(value: int) -> bool:
    return -_EXACT_FLOAT_INT_BOUND <= value <= _EXACT_FLOAT_INT_BOUND


# Bounds for the two nullable dtypes `fk_nullable_int_array` chooses between at
# FK write-back (see that function). Matches numpy's fixed-width int64/uint64
# range exactly -- these are not precision bounds like `_EXACT_FLOAT_INT_BOUND`
# above, they are storage bounds for the dtype pandas' `Int64`/`UInt64`
# extension arrays are backed by.
_INT64_MIN: Final = -(2**63)
_INT64_MAX: Final = 2**63 - 1
_UINT64_MAX: Final = 2**64 - 1

# The per-Arrow-integer-type nullable dtype map lives in `internal.pandas_compat` (generation
# needs the same conversion). Kept under this name so the route's patch point stays here.
# See this module's docstring "The lossless representation..." paragraph for why it is per-type.
_exact_fk_types_mapper = exact_int_types_mapper


def lossless_fk_int_values(values: Sequence[object]) -> list[int | None] | None:
    """Classify one resolved FK output column for lossless construction.

    `values` is one column's worth of already-resolved per-row values (e.g.
    `PandasExecutionAdapter._resolve_fk_node`'s per-row masked/preserved/
    remapped key components for one child FK column) -- `None` marks a null
    slot (a null source FK, or a cascaded quarantine row).

    Classification is by each value's OWN Python type, not by folding a
    whole-valued float to its equal int (`fk_key_value` does that folding for
    JOIN-KEY equality, a different question from "what dtype can this
    OUTPUT column use without corrupting anything"). A `float` value stays in
    the float bucket here even when it is whole-valued (e.g. `1.0`), because
    it carries a REAL float dtype from its source (typically a matched FK
    parent column that is itself float-typed) -- the out-of-core route's
    matched branch (`_join.py::_append_output_batch`) preserves that same
    parent value verbatim, undoing only its own orphan branch's
    `fk_key_value` fold. Matching that split here (fold only where
    out-of-core folds) is what keeps the two routes agreeing on exactly
    which shapes are lossless vs. must reject -- folding a matched float to
    int here would make the pandas route accept a mixed float/big-int column
    the out-of-core route still rejects (see the two tests this mirrors:
    `test_large_whole_float_orphan_int_parent_matches_oracle`, parity, and
    `test_matched_float_and_int_orphan_beyond_precision_fails_closed`, both
    routes reject).

    Returns a list of `int | None` when every non-null value is a genuine
    integer (`numbers.Integral`, excluding `bool`): the caller should build
    the output column from this list via `fk_nullable_int_array` (which picks
    the narrowest of pandas' nullable `Int64`/`UInt64` extension dtypes that
    holds every value exactly) instead of a raw list assignment, so a
    null-bearing integer column never falls back to numpy's float64 (which
    cannot exactly hold an integer beyond 2**53). `Int64` round-trips to plain
    Arrow `int64` and `UInt64` to plain Arrow `uint64` on
    `pa.Table.from_pandas`, so this changes the pandas-internal dtype only,
    not the eventual Arrow/Parquet output type for an integer FK column that
    was already null-free.

    Returns `None` when the column is not a pure integer-with-null shape (it
    holds strings/bools, or mixes a genuine float with integers that are ALL
    within exactly-representable float range) -- the caller's existing
    construction already has no precision to lose for that shape and should
    proceed unchanged.

    Raises `ExecutionError(code=FK_KEY_DTYPE_UNSUPPORTED_CODE)` when the
    column mixes a float value with an integer beyond exact float64
    precision (> 2**53): no single dtype (int64 truncates the fraction;
    float64 rounds the integer) can hold both without corrupting one of
    them. This is the exact shape the out-of-core route already rejects in
    `cast_fk_chunk`/`_append_output_batch` -- see
    tests/parity/SEMANTIC_DIFFERENCES.md, "Pandas FK oracle is NOT
    authoritative for an int key past float precision" -- so this raises the
    SAME code rather than silently picking a lossy dtype.
    """
    normalized: list[int | None] = []
    saw_non_int = False
    saw_unrepresentable_int = False
    for value in values:
        # `pd.isna` (not a bare `value is None` check) catches every pandas
        # missing sentinel a resolved key component can carry here: plain
        # `None` (the common case, an unmatched/null FK), `pd.NA` (a masked
        # value read back off a nullable `Int64` parent column -- see
        # `to_pandas_fk_safe`), and float `NaN`.
        if value is None:
            normalized.append(None)
            continue
        try:
            is_null = bool(pd.isna(value))
        except (TypeError, ValueError):
            # `pd.isna` returns an array (not a scalar bool) for an
            # array-like value, and `bool()` on that raises ValueError
            # ("truth value of an array is ambiguous"). Not reachable from
            # any scalar FK key component this engine resolves today (every
            # `_resolve_fk_node` caller passes one masked/preserved/remapped
            # scalar per row), but defensive: fall through to the "not a
            # pure int" bucket below instead of crashing, matching what the
            # pre-DE-10 bare list/array assignment did for this shape.
            is_null = False
        if is_null:
            normalized.append(None)
            continue
        # bool is a numbers.Integral subtype (True == 1); kept out of the
        # int bucket deliberately, matching the out-of-core route's own
        # "bool/int64 stays in the can't-merge bucket" rule
        # (`_join.py::_concat_fk_chunks`).
        if not isinstance(value, bool) and isinstance(value, numbers.Integral):
            int_value = int(value)
            if not _is_exactly_float_representable(int_value):
                saw_unrepresentable_int = True
            normalized.append(int_value)
            continue
        saw_non_int = True
        normalized.append(None)  # placeholder; discarded below (not int-pure)
    if not saw_non_int:
        return normalized
    if saw_unrepresentable_int:
        raise ExecutionError(
            code=FK_KEY_DTYPE_UNSUPPORTED_CODE,
            message=(
                "FK output column mixes a float value with an integer key "
                "beyond exactly-representable float64 precision (> 2**53); "
                "int64 would truncate the fraction and float64 would round the "
                "integer, so neither dtype can hold both without corrupting "
                "one of them."
            ),
        )
    return None


def fk_nullable_int_array(
    values: Sequence[int | None], *, table: str | None = None, column: str | None = None
) -> pd.api.extensions.ExtensionArray:
    """Build the pandas array for one resolved FK write-back column from
    `lossless_fk_int_values`'s classified `int | None` output.

    Chooses the narrowest of pandas' nullable `Int64`/`UInt64` extension
    dtypes that can hold every value exactly: `Int64` (signed, the prior
    hardcoded choice -- unchanged for the overwhelmingly common case) unless
    a value exceeds `Int64`'s range, in which case `UInt64` is used so a
    genuine unsigned key in `[2**63, 2**64)` (e.g. a preserved orphan read
    off a source `uint64` FK column -- the same shape `to_pandas_fk_safe` now
    ingests exactly) round-trips instead of raising a raw, uncoded
    `OverflowError` building a signed `Int64` array that cannot hold it. A
    value that fits in NEITHER (below `Int64`'s minimum, or above `UInt64`'s
    maximum -- not reachable from any FK source dtype this engine supports,
    but checked so a future one fails closed instead of corrupting) raises
    `FK_KEY_DTYPE_UNSUPPORTED_CODE`, the SAME code every other genuinely
    unrepresentable FK key shape raises, rather than surfacing pandas' raw
    construction error. Every message is fixed text naming `table`.`column`
    only: a resolved key is a real (or masked) value and never reaches it.

    Deliberately does NOT attempt to reproduce a NARROWER width (int8/16/32)
    than `Int64`/`UInt64`: this is the WRITE-BACK path for a RESOLVED key
    (masked, remapped, or preserved), which may legitimately differ in value
    from the child's own source width (e.g. a `hash`-remapped orphan), so
    there is no single "original width" to reproduce here the way ingestion
    reproduces the source column's own Arrow type. That is a pure schema
    (not correctness) question tracked separately; this function's job is
    exactness and never crashing uncoded, matching `to_pandas_fk_safe`'s
    ingestion-side crash fix for the same unsigned-past-`Int64` shape.
    """
    needs_uint64 = False
    for value in values:
        if value is None:
            continue
        if value > _UINT64_MAX:
            _raise_fk_range(table, column, "exceeds the UInt64 range")
        if value < _INT64_MIN:
            _raise_fk_range(table, column, "is below the Int64 range")
        needs_uint64 = needs_uint64 or value > _INT64_MAX
    dtype_name = "UInt64" if needs_uint64 else "Int64"
    # pandas' and Arrow's construction errors quote the offending key, so the
    # coded error is raised outside the handler with neither cause nor context.
    try:
        return pd.array(values, dtype=dtype_name)
    except (pa.lib.ArrowInvalid, OverflowError, TypeError, ValueError):
        pass
    _raise_fk_range(table, column, f"cannot share one lossless nullable {dtype_name} array")


def _raise_fk_range(table: str | None, column: str | None, reason: str) -> NoReturn:
    where = f" {table}.{column}" if table and column else ""
    raise ExecutionError(
        code=FK_KEY_DTYPE_UNSUPPORTED_CODE,
        message=f"FK output column{where}: a resolved key {reason}.",
    )


def fk_all_null_array(length: int, source_dtype: object) -> pd.api.extensions.ExtensionArray:
    """Build the pandas array for an all-null RESOLVED FK write-back column
    (DE-10 reland MEDIUM: `_pandas_adapter.py::_resolve_fk_node`).

    An all-null resolved column (every row cascaded/orphaned to null) has no
    integer value to lose precision on, so there is no correctness reason to
    force it through `fk_nullable_int_array`'s `Int64` default -- that would
    retype a null-bearing string/uint32/etc. SOURCE column to `Int64` in the
    output. `source_dtype` is the column's own pre-resolution dtype (already
    the exact per-Arrow-type nullable dtype for a `to_pandas_fk_safe`-
    protected column, e.g. `UInt32`, or pandas' own object/float64 default
    otherwise); this preserves it instead. Falls back to the `Int64` default
    only when `source_dtype` genuinely cannot hold an all-null column (a
    plain, non-nullable numpy dtype with no null representation)."""
    try:
        return pd.array([None] * length, dtype=source_dtype)
    except (TypeError, ValueError):
        return fk_nullable_int_array([None] * length)


def fk_columns_for_table(edges: Iterable[RelationshipEdge], table: str) -> set[str]:
    """Every column on `table` that is a relationship edge's parent or child
    key -- the set `to_pandas_fk_safe` must protect from float64-on-null
    widening. A table can be a parent, a child, or both (self-referential FK)."""
    cols: set[str] = set()
    for edge in edges:
        if edge.parent_table == table:
            cols.update(edge.parent_columns)
        if edge.child_table == table:
            cols.update(edge.child_columns)
    return cols


def to_pandas_fk_safe(table: pa.Table, fk_columns: Collection[str]) -> pd.DataFrame:
    """Convert one Arrow table to pandas, keeping every FK-relevant integer
    column exact.

    `table.to_pandas()` widens an Arrow integer column with ANY null to numpy
    float64 (there is no lossless numpy integer-with-null representation),
    which rounds any value beyond 2**53 before a single strategy has run --
    the pandas-route ingestion half of the DE-10 finding (the write-back half
    is `lossless_fk_int_values` / `fk_nullable_int_array`, applied where a
    resolved FK column is assigned back into the frame).

    `fk_columns` (from `fk_columns_for_table`) names every column on this
    table that is a relationship edge's parent or child key. Each one that is
    Arrow-integer-typed is re-read through pyarrow's `types_mapper` hook (the
    supported way to opt a column into one of pandas' nullable integer
    extension dtypes instead of the float64-on-null default -- see the
    pyarrow `to_pandas` / "Nullable Types" documentation) using
    `_exact_fk_types_mapper`, which maps EACH Arrow integer type to its OWN
    same-width, same-signedness nullable dtype (`int32` -> `Int32`, `uint64`
    -> `UInt64`, etc.) rather than a single blanket signed `Int64`. This keeps
    a null-bearing integer column exact (including a `uint64` key in
    `[2**63, 2**64)`, which a blanket `Int64` cast cannot hold at all) AND
    preserves its original width (a narrower key, e.g. an `int32`
    auto-increment PK, no longer widens to `int64` in the output) -- each
    dtype round-trips to its OWN matching plain Arrow type on the way back
    out (`pa.Table.from_pandas`).

    Every OTHER column -- anything not named in `fk_columns` -- is UNCHANGED
    (still the pyarrow default). This is a targeted fix for FK
    referential-integrity data, not a blanket dtype policy change for every
    integer column the engine masks.

    A `pyarrow.lib.ArrowInvalid` or `OverflowError` from the cast itself (a
    value that genuinely does not fit even its own Arrow type's matching
    nullable dtype -- not reachable from a well-formed Arrow table, but a
    fail-closed backstop rather than an assumption) is re-raised as
    `ExecutionError(code=FK_KEY_DTYPE_UNSUPPORTED_CODE)`, the SAME code every
    other route raises for a genuinely unrepresentable FK key, instead of
    surfacing an uncoded crash unique to this route.
    """

    def unsupported(col: str) -> NoReturn:
        raise ExecutionError(
            code=FK_KEY_DTYPE_UNSUPPORTED_CODE,
            message=f"FK column {col!r} could not be ingested as a lossless nullable integer.",
        )

    return table_to_pandas_exact_ints(
        table,
        [col for col in fk_columns if col in table.column_names],
        types_mapper=_exact_fk_types_mapper,
        on_unconvertible=unsupported,
    )


__all__ = [
    "FK_KEY_DTYPE_UNSUPPORTED_CODE",
    "NULL_FK_KEY",
    "fk_all_null_array",
    "fk_columns_for_table",
    "fk_join_key",
    "fk_join_key_tuple",
    "fk_key_value",
    "fk_nullable_int_array",
    "lossless_fk_int_values",
    "to_pandas_fk_safe",
]

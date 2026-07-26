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

import decimal
import math
import numbers
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Final

import pandas as pd
import pyarrow as pa

from decoy_engine.execution._errors import ExecutionError

if TYPE_CHECKING:
    from decoy_engine.relationships._graph import RelationshipEdge


@dataclass(frozen=True)
class _NullFkKey:
    pass


NULL_FK_KEY: Final = _NullFkKey()


def fk_key_value(value: object) -> object:
    """Normalize one FK key component to match pandas parent-map semantics."""
    if value is None:
        return NULL_FK_KEY
    if isinstance(value, bool):
        # The oracle's parent_map is a plain Python dict; `True`/`False` hash
        # and compare equal to `1`/`0` (bool is an int subtype), so a bool
        # parent key and a 0/1 int child key collide there for free. This
        # normalization makes that same collision reachable through
        # `fk_join_key`'s string encoding (which cannot rely on Python's
        # object equality), so a bool parent key and a 0/1 int child key mint
        # the identical join token the oracle's dict already produces.
        return int(value)
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, float):
        if math.isnan(value):
            return NULL_FK_KEY
        return int(value) if value.is_integer() else value
    if isinstance(value, numbers.Number):
        # Non-Integral, non-float numeric types (decimal.Decimal is the
        # reachable one; it registers as numbers.Number but not
        # numbers.Integral/Real) that Python's own == / hash already fold
        # onto int above: hash(Decimal("1")) == hash(1) and Decimal("1") == 1
        # (the PEP 3141 numeric tower), which is exactly the equality a plain
        # dict-keyed parent_map (the pandas adapter's _parent_map) relies on
        # without ever calling this function's int/float branches. Collapse
        # the whole-valued case the same way the float branch above does, so
        # the join-key encoder's token matches; a NaN-valued Number collapses
        # to NULL_FK_KEY the same way float NaN does (pandas' `pd.isna`
        # treats both as missing). A genuinely fractional value (e.g.
        # Decimal("2.5")) is returned unchanged rather than guessed at: it
        # stays distinct from any int it is not equal to, so this never
        # collapses two values full-frame would keep apart.
        try:
            is_nan = value != value  # only NaN is unequal to itself
        except Exception:
            return value
        if is_nan:
            return NULL_FK_KEY
        try:
            # numbers.Number does not declare __int__/__trunc__ in the
            # typeshed stubs (only its Integral/Real/Rational subtypes do),
            # so the static type is widened to Any here; the runtime
            # TypeError branch below still catches a Number that has no
            # such conversion (e.g. complex).
            as_int = int(value)  # type: ignore[call-overload]
        except (TypeError, ValueError, OverflowError):
            return value
        return as_int if value == as_int else value
    return value


# Explicit, wide-precision context for `_decimal_join_token`'s `.normalize()`
# call: normalize() rounds using its context's precision, and the *ambient*
# default context caps at 28 significant digits. Arrow's decimal128/decimal256
# key types go up to 38/76 significant digits, so normalizing under the
# default context could silently round a legitimate high-precision key -- a
# correctness bug worse than the one being fixed. 200 digits is comfortably
# above decimal256's ceiling with headroom, so for a Decimal-typed key
# normalize() only ever strips a trailing-zero exponent, never rounds.
#
# The RI fix (float branch of `fk_join_key`) also routes floats here via
# `Decimal(a_float)`, whose EXACT expansion CAN exceed 200 digits (a subnormal
# like `Decimal(5e-324)` is ~751 digits and IS rounded to 200 here). That does
# not break the fold's injectivity: two distinct IEEE doubles differ within
# ~17 significant decimal digits, far inside 200, so their rounded tokens stay
# distinct; and an equal float/Decimal pair has a short exact expansion that
# never rounds. So a float can round here without ever colliding with a
# different key -- rounding only trims digits both members already share.
_DECIMAL_JOIN_CONTEXT: Final = decimal.Context(prec=200)


def _decimal_join_token(value: Decimal) -> str:
    """Canonical join-token text for a Decimal, matching the oracle's dict.

    `Decimal('1.20') == Decimal('1.2')` and they hash equal -- the decimal
    module deliberately satisfies the hash/eq contract across exponent-only
    (trailing-zero) differences -- so the pandas oracle's plain-dict
    `parent_map` already resolves a parent masked under one scale and a child
    read under another as the SAME key. `repr()` does not: it renders the
    coefficient and exponent verbatim, so those two equal Decimals would mint
    different DuckDB join tokens without this step. `.normalize()` collapses
    the exponent difference (`Decimal('1.20').normalize() ==
    Decimal('1.2').normalize()`, and both repr identically); zero is also
    canonicalized to a non-negative sign, since `Decimal('-0') ==
    Decimal('0')` but `.normalize()` alone preserves an all-zero value's sign.
    This is a JOIN-KEY-ONLY normalization: `fk_key_value`'s return value (what
    PRESERVE/WARN write back for an orphan) is untouched, so a preserved
    child keeps its own source scale exactly like the oracle does.
    """
    canonical = value.normalize(context=_DECIMAL_JOIN_CONTEXT)
    if canonical.is_zero():
        canonical = abs(canonical)
    return repr(canonical)


def fk_join_key(value: object) -> str:
    """Encode one normalized FK key component for relational joins."""
    normalized = fk_key_value(value)
    if normalized is NULL_FK_KEY:
        return "\x00NULL"
    # `fk_key_value` normalizes bool to int above, so `normalized` is never a
    # bool here; no separate BOOL: tag is needed (or correct -- it would give
    # bool and 0/1 int keys different tokens, undoing the normalization).
    if isinstance(normalized, int):
        return f"\x00INT:{normalized}"
    if isinstance(normalized, float):
        # RI fix (2026-07-25, Codex-confirmed): a FRACTIONAL float and an
        # equal-valued fractional Decimal must mint the SAME join token, because
        # the pandas oracle's plain-dict parent_map already treats them as one
        # key whenever Python's numeric tower says they are equal
        # (`12.5 == Decimal("12.5")` is True). Type-tagging them apart
        # (`\x00FLOAT:` vs `\x00DEC:`) made the out-of-core route disagree with
        # the dict route -- a real referential-integrity divergence (a valid
        # child looked like an orphan on one route only). Encode the float
        # through its EXACT decimal expansion and the same `_decimal_join_token`
        # a Decimal uses, so the token matches IFF the values are truly equal:
        # `Decimal(12.5)` is `Decimal('12.5')` (same token as `Decimal("12.5")`),
        # while `Decimal(0.1)` is the exact 0.1000...0625 expansion (a different
        # token from `Decimal("0.1")`, matching that `0.1 != Decimal("0.1")` in
        # Python). Infinity round-trips through Decimal cleanly; NaN never
        # reaches here (fk_key_value folds it to NULL_FK_KEY).
        return f"\x00DEC:{_decimal_join_token(Decimal(normalized))}"
    if isinstance(normalized, str):
        return f"\x00STR:{len(normalized)}:{normalized}"
    if isinstance(normalized, Decimal):
        # A fractional Decimal (not int-equal, see `fk_key_value` above) is
        # returned unchanged by `fk_key_value` so PRESERVE/WARN keep the
        # child's own scale; the join token still must fold scale-only
        # differences the oracle's dict already folds for free.
        return f"\x00DEC:{_decimal_join_token(normalized)}"
    return f"\x00OBJ:{type(normalized).__qualname__}:{normalized!r}"


def fk_join_key_tuple(values: tuple[object, ...]) -> str:
    """Encode a full FK key tuple for relational joins.

    Same injective length-prefixed framing idea as `_encode_int`'s ASN.1 DER
    (X.690 §8.3) length-prefix lineage (kernel/_canonicalize.py): prefixing
    each component with its own length keeps two differently-shaped key
    tuples from concatenating into the same joined string.
    """
    parts = [fk_join_key(value) for value in values]
    return "".join(f"{len(part)}:{part}" for part in parts)


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
FK_KEY_DTYPE_UNSUPPORTED_CODE: Final = "out_of_core_fk_key_dtype_unsupported"

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

# Every Arrow integer type `to_pandas_fk_safe` may see on an FK column, mapped
# to its OWN same-width, same-signedness nullable pandas extension dtype --
# see this module's docstring "The lossless representation..." paragraph for
# why this must be per-type rather than a single blanket `Int64`.
_ARROW_INT_TO_NULLABLE_DTYPE: Final[dict[pa.DataType, type[pd.api.extensions.ExtensionDtype]]] = {
    pa.int8(): pd.Int8Dtype,
    pa.int16(): pd.Int16Dtype,
    pa.int32(): pd.Int32Dtype,
    pa.int64(): pd.Int64Dtype,
    pa.uint8(): pd.UInt8Dtype,
    pa.uint16(): pd.UInt16Dtype,
    pa.uint32(): pd.UInt32Dtype,
    pa.uint64(): pd.UInt64Dtype,
}


def _exact_fk_types_mapper(arrow_type: pa.DataType) -> pd.api.extensions.ExtensionDtype | None:
    """`to_pandas(types_mapper=...)` hook: exact per-Arrow-integer-type
    nullable pandas dtype, preserving width and signedness (closes BLOCKER #1
    / HIGH #2 / MEDIUM #3 -- see module docstring). Returns `None` for a
    non-integer type or an integer width/signedness this contract does not
    special-case, so `to_pandas_fk_safe`'s caller (already gated on
    `pa.types.is_integer`) falls back to pyarrow's own default for anything
    this map does not cover, rather than raising."""
    dtype_cls = _ARROW_INT_TO_NULLABLE_DTYPE.get(arrow_type)
    return dtype_cls() if dtype_cls is not None else None


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


def fk_nullable_int_array(values: Sequence[int | None]) -> pd.api.extensions.ExtensionArray:
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
    construction error.

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
        if value > _INT64_MAX:
            if value > _UINT64_MAX:
                raise ExecutionError(
                    code=FK_KEY_DTYPE_UNSUPPORTED_CODE,
                    message=(
                        f"FK output column has a resolved key value {value} that "
                        "exceeds UInt64's maximum representable value "
                        f"({_UINT64_MAX}); no lossless dtype can hold it."
                    ),
                )
            needs_uint64 = True
        elif value < _INT64_MIN:
            raise ExecutionError(
                code=FK_KEY_DTYPE_UNSUPPORTED_CODE,
                message=(
                    f"FK output column has a resolved key value {value} that is "
                    f"below Int64's minimum representable value ({_INT64_MIN}); "
                    "no lossless dtype can hold it."
                ),
            )
    dtype_name = "UInt64" if needs_uint64 else "Int64"
    try:
        return pd.array(values, dtype=dtype_name)
    except (pa.lib.ArrowInvalid, OverflowError, TypeError, ValueError) as exc:
        raise ExecutionError(
            code=FK_KEY_DTYPE_UNSUPPORTED_CODE,
            message=(
                f"FK output column could not be built as a lossless nullable "
                f"{dtype_name} array: {exc}"
            ),
        ) from exc


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
    df = table.to_pandas()
    for col in fk_columns:
        if col not in table.column_names:
            continue
        arrow_type = table.schema.field(col).type
        if not pa.types.is_integer(arrow_type):
            continue
        try:
            fk_series = table.column(col).to_pandas(types_mapper=_exact_fk_types_mapper)
            # Positional, not label-aligned: `df` (from `table.to_pandas()`)
            # keeps whatever pandas index the Arrow table's pandas metadata
            # carries (e.g. a real parquet file's original row labels), while
            # `fk_series` always gets a fresh default RangeIndex from this
            # column-only `to_pandas()` call. `df[col] = fk_series` aligns by
            # LABEL, not position -- for any table whose index is not already
            # `[0, 1, 2, ...]` (duplicate labels, a shuffled/non-default
            # index, or labels absent from the fresh RangeIndex) that silently
            # nulls, swaps, or duplicates values instead of raising. A pandas
            # `ExtensionArray` (`.array`, not `.values`) carries no index at
            # all, so assigning it is always positional regardless of `df`'s
            # index.
            df[col] = fk_series.array
        except (pa.lib.ArrowInvalid, OverflowError) as exc:
            raise ExecutionError(
                code=FK_KEY_DTYPE_UNSUPPORTED_CODE,
                message=(
                    f"FK column {col!r} (Arrow type {arrow_type}) could not be cast "
                    "to its matching lossless nullable pandas dtype during "
                    f"ingestion: {exc}"
                ),
            ) from exc
    return df


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

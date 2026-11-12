"""SS2: seed selection -- sample / filter / keys.

Pattern: bottom-k / KMV consistent sampling (same family as MinHash bottom-k
sketches), keyed with HMAC-SHA256 (RFC 2104) instead of an unkeyed hash so
selection is reproducible per job seed. `sample` mode selects rows by their
smallest keyed-hash value, which is stable under reruns and independent of
any RNG library's internal state. `pl.DataFrame.sample(seed=...)` was
REJECTED for this: it is only guaranteed reproducible within one polars
version, and the engine's determinism envelope (cross-sprint contracts R3)
requires cross-version stability -- exactly what HMAC-over-canonical-bytes
already provides for every masked value (`decoy_engine.determinism`,
`_canonicalize_source`).

`filter` mode maps structured `Predicate`s to `pl.Expr` and AND-reduces them.
No string-eval surface: unlike the `expressions`/lark parser (pandas-row
oriented), structured comparisons map 1:1 onto `pl.Expr` with no parsing step.

`keys` mode semi-joins an explicit key list against the key frame. Raw key
values live ONLY on `SeedSpec.keys`; they are never serialized (see
`_api.py`'s `seed_specs_public` construction).
"""

from __future__ import annotations

import math
import operator
from collections.abc import Mapping
from datetime import date, datetime, time
from decimal import Decimal
from fractions import Fraction
from functools import reduce
from math import floor
from typing import Any

import numpy as np
import polars as pl

from decoy_engine import reasons
from decoy_engine.determinism._derive import DeriveContext
from decoy_engine.generation.pool._canonicalize import _canonicalize_source
from decoy_engine.generation.pool._errors import GenerationError
from decoy_engine.subset._errors import SubsetConfigError
from decoy_engine.subset._keys import RI
from decoy_engine.subset._types import Predicate, SeedSpec

_OPS = {
    "eq": operator.eq,
    "ne": operator.ne,
    "lt": operator.lt,
    "le": operator.le,
    "gt": operator.gt,
    "ge": operator.ge,
}


def _predicate_expr(p: Predicate, kf: pl.DataFrame, table: str) -> pl.Expr:
    c = pl.col(p.column)
    if p.op == "is_null":
        return c.is_null()
    if p.op == "is_not_null":
        return c.is_not_null()
    target = kf.schema[p.column]
    try:
        expr = _literal_predicate_expr(c, p, target)
    except _LITERAL_ERRORS:
        expr = None
    if expr is None:
        # Same boundary as explicit keys: polars' error text echoes the literal.
        # Raised outside the handler so no chained cause or context carries it.
        raise SubsetConfigError(
            code=reasons.config.SUBSET_SEED_KEY_TYPE,
            message=f"table {table!r}: filter literal(s) for column {p.column!r} do not "
            f"convert exactly to the column's {_dtype_label(target)} type",
        )
    return expr


def _literal_predicate_expr(c: pl.Expr, p: Predicate, target: pl.DataType) -> pl.Expr:
    values = list(p.value) if p.op == "in" else [p.value]
    if p.op in _ORDERING and _is_decimal_on_float(p.value, target):
        # Decimal("0.1") round-trips through Float64 0.1 but is below it.
        return _range_expr(c, p.op, p.value, target)
    try:
        literal = _literals_exact(values, target)
    except _LITERAL_ERRORS:
        if p.op not in _ORDERING:
            raise
        bridged = _naive_temporal_range(c, p.op, p.value, target)
        if bridged is not None:
            return bridged
        if not _is_real_bound(p.value, target):
            raise
        return _range_expr(c, p.op, p.value, target)
    if p.op == "in":
        return c.is_in(literal.implode())
    return _OPS[p.op](c, pl.lit(literal).first())


_ORDERING = frozenset({"lt", "le", "gt", "ge"})
_INT_BITS: dict[type[pl.DataType], tuple[int, bool]] = {
    pl.Int8: (8, True),
    pl.Int16: (16, True),
    pl.Int32: (32, True),
    pl.Int64: (64, True),
    pl.UInt8: (8, False),
    pl.UInt16: (16, False),
    pl.UInt32: (32, False),
    pl.UInt64: (64, False),
}
_FLOAT_NP: dict[type[pl.DataType], type[np.floating[Any]]] = {
    pl.Float32: np.float32,
    pl.Float64: np.float64,
}


def _is_real_bound(value: object, target: pl.DataType) -> bool:
    """An int, float or Decimal literal (never bool) against an int, float or Decimal column."""
    if type(value) not in (int, float, Decimal):
        return False
    if isinstance(value, Decimal) and not value.is_finite():
        return False
    base = target.base_type()
    return base in _INT_BITS or base in _FLOAT_NP or base is pl.Decimal


def _is_decimal_on_float(value: object, target: pl.DataType) -> bool:
    return type(value) is Decimal and value.is_finite() and target.base_type() in _FLOAT_NP


def _naive_temporal_range(
    c: pl.Expr, op: str, value: object, target: pl.DataType
) -> pl.Expr | None:
    """A naive datetime bound on a Date column, or a date bound on a naive Datetime column.

    The date is compared as its midnight, as polars' own supertype cast does
    (Date -> Datetime). Aware values keep the timezone refusal.
    """
    if target == pl.Date and type(value) is datetime and value.tzinfo is None:
        us = pl.Datetime("us")  # Python datetimes are microsecond values: exact
        return _OPS[op](c.cast(us), pl.lit(value, dtype=us))
    if isinstance(target, pl.Datetime) and target.time_zone is None and type(value) is date:
        bound = _literals_exact([datetime.combine(value, time())], target)
        return _OPS[op](c, pl.lit(bound).first())
    return None


# |exponent| past every dtype's reach: Float64 tops out near 1e308 and its
# smallest subnormal is near 5e-324; polars Decimal holds 38 digits at scale <= 38.
_EXP_CLAMP = 400


def _exact_bound(value: float | Decimal) -> Fraction:
    """`value` as an exact rational; a Decimal exponent is clamped first.

    Fraction(Decimal("1E+999999999")) builds a billion-digit integer. Any
    exponent beyond `_EXP_CLAMP` orders the same against every dtype value as
    10**±_EXP_CLAMP of the same sign, so the clamp changes no comparison.
    """
    if isinstance(value, Decimal) and value and abs(value.adjusted()) > _EXP_CLAMP:
        sign = -1 if value.is_signed() else 1
        if value.adjusted() > 0:
            return Fraction(sign * 10**_EXP_CLAMP)
        return Fraction(sign, 10**_EXP_CLAMP)
    return Fraction(value)


def _grid(target: pl.DataType) -> tuple[int, int, int]:
    """(scale, lowest k, highest k): an int or Decimal dtype holds exactly k / 10**scale."""
    base = target.base_type()
    if base in _INT_BITS:
        bits, signed = _INT_BITS[base]
        lo, hi = (-(2 ** (bits - 1)), 2 ** (bits - 1) - 1) if signed else (0, 2**bits - 1)
        return 0, lo, hi
    if not isinstance(target, pl.Decimal) or target.precision is None:
        raise TypeError("not an integer or Decimal dtype")
    top = 10**target.precision - 1
    return target.scale, -top, top


def _grid_lit(k: int, scale: int, target: pl.DataType) -> pl.Expr:
    if isinstance(target, pl.Decimal):
        # A string builds the Decimal exactly; arithmetic would round to 28 digits.
        return pl.lit(Decimal(f"{k}E-{scale}"), dtype=target)
    return pl.lit(k, dtype=target)


def _range_expr(c: pl.Expr, op: str, value: float | Decimal, target: pl.DataType) -> pl.Expr:
    """`c <op> value` for a bound the column dtype cannot hold exactly.

    Pattern: exact rational comparison (Python's own int/float/Decimal
    ordering, which compares the true values; CPython `float_richcompare`).
    The bound is moved to the nearest value of the column's dtype on the
    correct side, computed with `fractions.Fraction`, so nothing rounds:
    `c < 1.5` on integers is `c <= 1`, `c < 1.005` on Decimal(10, 2) is
    `c <= 1.00`, `c < 2**53 + 1` on Float64 is `c <= 2**53`. A bound beyond
    the dtype's range selects every non-null row or none.
    """
    upper = op in ("lt", "le")  # the column must lie below the bound
    if (isinstance(value, Decimal) and value.is_nan()) or (
        isinstance(value, float) and math.isnan(value)
    ):
        raise ValueError("NaN range bound")
    base = target.base_type()
    if isinstance(value, float) and math.isinf(value):
        below_all = value < 0
        return c.is_not_null() if upper != below_all else pl.lit(False)
    bound = _exact_bound(value)
    if base in _FLOAT_NP:
        edge_f = _float_edge(bound, _FLOAT_NP[base], down=upper)
        if math.isfinite(edge_f) and Fraction(edge_f) == bound:
            return _OPS[op](c, pl.lit(edge_f, dtype=target))
        # The bound is not a dtype value, so lt == le and gt == ge.
        return c <= pl.lit(edge_f, dtype=target) if upper else c >= pl.lit(edge_f, dtype=target)
    scale, lo, hi = _grid(target)
    scaled = bound * 10**scale
    if upper:
        edge = math.ceil(scaled) - 1 if op == "lt" else math.floor(scaled)
        if edge < lo:
            return pl.lit(False)
        return c.is_not_null() if edge >= hi else c <= _grid_lit(edge, scale, target)
    edge = math.floor(scaled) + 1 if op == "gt" else math.ceil(scaled)
    if edge > hi:
        return pl.lit(False)
    return c.is_not_null() if edge <= lo else c >= _grid_lit(edge, scale, target)


def _float_edge(bound: Fraction, ftype: type[np.floating[Any]], *, down: bool) -> float:
    """The largest dtype float <= `bound` (`down`), else the smallest >= it.

    Infinity is a dtype value, so a bound beyond the finite range lands on
    the largest finite value or on an infinity, which keeps the comparison
    exact for infinite column values too.
    """
    with np.errstate(over="ignore"):
        try:
            f = ftype(float(bound))
        except OverflowError:
            f = ftype(math.inf if bound > 0 else -math.inf)

    def ok(x: np.floating[Any]) -> bool:
        if math.isinf(x):
            return bool(x < 0) if down else bool(x > 0)
        return Fraction(float(x)) <= bound if down else Fraction(float(x)) >= bound

    step = ftype(-math.inf if down else math.inf)
    while not ok(f):
        f = np.nextafter(f, step)
    return float(f)


# The reason is chosen by code, never by the exception text, so the message stays a closed set.
_UNCANONICALIZABLE_REASON: dict[str, str] = {
    reasons.runtime.FLOAT_CANONICALIZATION_UNSUPPORTED: "float values are not supported",
    reasons.runtime.TIMEZONE_NAIVE_DATETIME: "timezone-naive datetimes are not supported",
}


def _uncanonicalizable(spec: SeedSpec, position: int, code: str) -> SubsetConfigError:
    reason = _UNCANONICALIZABLE_REASON.get(code, "the value type is not supported")
    return SubsetConfigError(
        code=reasons.config.SUBSET_SEED_KEY_UNCANONICALIZABLE,
        message=f"table {spec.table!r}: seed key column {spec.key_columns[position]!r} holds "
        f"a value that cannot be canonicalized for sampling: {reason}",
    )


def _select_sample(spec: SeedSpec, kf: pl.DataFrame, job_seed: bytes) -> tuple[frozenset[int], int]:
    try:
        ns = f"subset/sample/{spec.table}"
        ctx = DeriveContext.for_column(job_seed, ns)
        kf = kf.select([RI, *spec.key_columns])
        non_null = kf.drop_nulls(subset=list(spec.key_columns))
        seed_null_excluded = kf.height - non_null.height
        n = non_null.height
        if n == 0:
            return frozenset(), seed_null_excluded
        k = spec.count if spec.count is not None else max(1, floor((spec.fraction or 0) * n))
        k = min(k, n)

        digests: list[tuple[bytes, int]] = []
        for row in non_null.iter_rows():
            ri, key = row[0], row[1:]
            parts: list[bytes] = []
            for position, component in enumerate(key):
                try:
                    part = _canonicalize_source(component)
                except GenerationError as exc:
                    rejected: tuple[int, str] | None = (position, exc.code)
                    break
                parts.append(len(part).to_bytes(4, "big") + part)
            else:
                rejected = None
            if rejected is not None:
                raise _uncanonicalizable(spec, *rejected)
            digests.append((ctx.derive_source(ns, b"".join(parts)), ri))
        digests.sort()
        selected = frozenset(ri for _, ri in digests[:k])
        return selected, seed_null_excluded
    except BaseException:
        del job_seed
        raise


def _select_filter(spec: SeedSpec, kf: pl.DataFrame) -> frozenset[int]:
    expr = reduce(operator.and_, (_predicate_expr(p, kf, spec.table) for p in spec.predicates))
    return frozenset(kf.filter(expr)[RI].to_list())


def _dtype_label(dtype: pl.DataType) -> str:
    """A closed, value-free name for `dtype`; an Enum's repr lists its categories."""
    if isinstance(dtype, pl.Enum):
        return "enum"
    if dtype == pl.Boolean:
        return "boolean"
    if dtype.is_integer():
        return "integer"
    if dtype.is_float():
        return "float"
    if dtype == pl.String or isinstance(dtype, pl.Categorical):
        return "string"
    if dtype == pl.Date:
        return "date"
    if isinstance(dtype, pl.Datetime):
        return "datetime"
    return "other"


def _value_family(dtype: pl.DataType) -> tuple[object, str | None]:
    if dtype == pl.Boolean:
        return "bool", None
    if dtype.is_integer():
        return "int", None
    if dtype.is_float():
        return "float", None
    if dtype == pl.String:
        return "str", None
    if isinstance(dtype, pl.Datetime):
        return pl.Datetime, dtype.time_zone
    return dtype.base_type(), None


def _column_accepts(target: pl.DataType, family: tuple[object, str | None]) -> bool:
    kind, tz = family
    if target == pl.Boolean:
        return kind == "bool"
    if target.is_numeric():
        return kind in ("int", "float") or kind is pl.Decimal
    if target == pl.String or isinstance(target, (pl.Categorical, pl.Enum)):
        return kind == "str"
    if isinstance(target, pl.Datetime):
        return kind is pl.Datetime and tz == target.time_zone
    return kind == target.base_type()


_LITERAL_ERRORS = (pl.exceptions.PolarsError, TypeError, ValueError, OverflowError)


def _literal_series(value: object) -> pl.Series:
    """A one-value Series for `value`, or a `_LITERAL_ERRORS` error.

    polars panics on a non-finite Decimal (a PanicException is a
    BaseException, so no handler here would see it) and raises a bare
    RuntimeError for a Decimal beyond Decimal128.
    """
    if isinstance(value, Decimal) and not value.is_finite():
        raise ValueError("non-finite Decimal literal")
    try:
        return pl.Series([value], strict=True)
    except RuntimeError as exc:
        raise ValueError("literal outside the polars range") from exc


def _literals_exact(values: list[Any], target: pl.DataType) -> pl.Series:
    """Convert operator literals (explicit keys, filter values) to `target`, or raise.

    Pattern: pyarrow's `cast(safe=True)` / numpy `can_cast(casting="safe")`,
    applied as a round-trip check. Each ORIGINAL value's dtype is inferred on
    its own (a multi-value Series infers from its first value and coerces the
    rest: True -> 1, a second timezone -> the first), so the family and
    timezone checks are order-independent. All non-null values must share one
    family (bool, int, float, str, datetime+tz, ...) that the column accepts:
    bool/str vs number are refused ("007" and "7" both parse to 7) and so is
    a timezone mismatch, which polars casts by relabelling. Each value is then
    cast strictly to `target`, cast back, and must equal the original, which
    refuses truncation (31337.4242 -> 31337), overflow and float precision
    loss (2**53 + 1, 0.1 -> Float32). The result keeps the input's positions
    (a null stays where it was), so explicit key columns built one at a time
    still line up row by row.
    """
    family: tuple[object, str | None] | None = None
    parts: list[pl.Series] = []
    for value in values:
        if value is None:
            parts.append(pl.Series([None], dtype=target))
            continue
        src = _literal_series(value)
        value_family = _value_family(src.dtype)
        if family is None:
            family = value_family
        if value_family != family or not _column_accepts(target, value_family):
            raise TypeError("literal family mismatch")
        cast = src.cast(target, strict=True)
        if not cast.cast(src.dtype, strict=True).eq_missing(src).all():
            raise ValueError("lossy literal cast")
        parts.append(cast)
    return pl.concat(parts) if parts else pl.Series([], dtype=target)


def _select_keys(spec: SeedSpec, kf: pl.DataFrame) -> frozenset[int]:
    columns: list[pl.Series] = []
    for i, c in enumerate(spec.key_columns):
        try:
            col = _literals_exact([tup[i] for tup in spec.keys], kf.schema[c])
        except _LITERAL_ERRORS:
            col = None
        if col is None:
            # The cast error text echoes key values; name only table and column, with
            # no chained cause or context.
            raise SubsetConfigError(
                code=reasons.config.SUBSET_SEED_KEY_TYPE,
                message=f"table {spec.table!r}: explicit seed key(s) for key column {c!r} "
                f"do not convert exactly to the column's {_dtype_label(kf.schema[c])} type",
            )
        columns.append(col.alias(c))
    # Engine null-key rule (`fk_join_key_tuple` callers skip any tuple with a
    # NULL_FK_KEY part): a requested tuple with a null part matches no row.
    key_df = pl.DataFrame(columns).drop_nulls()
    matched = kf.join(
        key_df, left_on=list(spec.key_columns), right_on=list(spec.key_columns), how="semi"
    )
    return frozenset(matched[RI].to_list())


def select_seed_rows(
    *,
    seeds: tuple[SeedSpec, ...],
    key_frames: Mapping[str, pl.DataFrame],
    job_seed: bytes,
) -> tuple[dict[str, frozenset[int]], dict[str, int], dict[str, int]]:
    """Resolve every `SeedSpec` into per-table seed row-index sets.

    Returns `(seed_rows, seed_counts, seed_null_excluded)`, all keyed by
    table. Two specs targeting the same table is a config error (union
    semantics across multiple specs on one table are a follow-on, not built
    here).
    """
    try:
        seen_tables: set[str] = set()
        seed_rows: dict[str, frozenset[int]] = {}
        seed_null_excluded: dict[str, int] = {}
        for spec in seeds:
            if spec.table in seen_tables:
                raise SubsetConfigError(
                    code=reasons.config.SUBSET_DUPLICATE_SEED_TABLE,
                    message=f"table {spec.table!r} has more than one SeedSpec; only one seed "
                    "spec per table is supported",
                )
            seen_tables.add(spec.table)
            kf = key_frames[spec.table]
            null_excluded = 0
            if spec.mode == "sample":
                selected, null_excluded = _select_sample(spec, kf, job_seed)
            elif spec.mode == "filter":
                selected = _select_filter(spec, kf)
            elif spec.mode == "head":
                selected = frozenset()
            else:
                selected = _select_keys(spec, kf)
            seed_rows[spec.table] = selected
            seed_null_excluded[spec.table] = null_excluded

        seed_counts = {table: len(rows) for table, rows in seed_rows.items()}
        return seed_rows, seed_counts, seed_null_excluded
    except BaseException:
        del job_seed
        raise

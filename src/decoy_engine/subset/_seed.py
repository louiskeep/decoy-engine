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

import operator
from collections.abc import Mapping
from functools import reduce
from math import floor
from typing import Any

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
    values = list(p.value) if p.op == "in" else [p.value]
    try:
        literal = _literals_exact(values, target)
    except _LITERAL_ERRORS as exc:
        # Same boundary as explicit keys: polars' error text echoes the literal.
        raise SubsetConfigError(
            code=reasons.config.SUBSET_SEED_KEY_TYPE,
            message=f"table {table!r}: filter literal(s) for column {p.column!r} do not "
            f"convert exactly to the column's {_dtype_label(target)} type",
        ) from exc
    if p.op == "in":
        return c.is_in(literal.implode())
    return _OPS[p.op](c, pl.lit(literal).first())


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
            try:
                source = b"".join(
                    len(part := _canonicalize_source(component)).to_bytes(4, "big") + part
                    for component in key
                )
            except GenerationError as exc:
                raise SubsetConfigError(
                    code=reasons.config.SUBSET_SEED_KEY_UNCANONICALIZABLE,
                    message=f"table {spec.table!r}: a seed key column value could not "
                    "be canonicalized for sampling",
                ) from exc
            digests.append((ctx.derive_source(ns, source), ri))
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
    loss (2**53 + 1, 0.1 -> Float32).
    """
    family: tuple[object, str | None] | None = None
    parts = [pl.Series([None] * sum(v is None for v in values), dtype=target)]
    for value in values:
        if value is None:
            continue
        src = pl.Series([value], strict=True)
        value_family = _value_family(src.dtype)
        if family is None:
            family = value_family
        if value_family != family or not _column_accepts(target, value_family):
            raise TypeError("literal family mismatch")
        cast = src.cast(target, strict=True)
        if not cast.cast(src.dtype, strict=True).eq_missing(src).all():
            raise ValueError("lossy literal cast")
        parts.append(cast)
    return pl.concat(parts)


def _select_keys(spec: SeedSpec, kf: pl.DataFrame) -> frozenset[int]:
    columns: list[pl.Series] = []
    for i, c in enumerate(spec.key_columns):
        try:
            col = _literals_exact([tup[i] for tup in spec.keys], kf.schema[c])
        except _LITERAL_ERRORS as exc:
            # The cast error text echoes key values; name only table and column.
            raise SubsetConfigError(
                code=reasons.config.SUBSET_SEED_KEY_TYPE,
                message=f"table {spec.table!r}: explicit seed key(s) for key column {c!r} "
                f"do not convert exactly to the column's {_dtype_label(kf.schema[c])} type",
            ) from exc
        columns.append(col.alias(c))
    key_df = pl.DataFrame(columns)
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
            else:
                selected = _select_keys(spec, kf)
            seed_rows[spec.table] = selected
            seed_null_excluded[spec.table] = null_excluded

        seed_counts = {table: len(rows) for table, rows in seed_rows.items()}
        return seed_rows, seed_counts, seed_null_excluded
    except BaseException:
        del job_seed
        raise

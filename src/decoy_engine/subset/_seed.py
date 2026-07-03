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

import polars as pl

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


def _predicate_expr(p: Predicate) -> pl.Expr:
    c = pl.col(p.column)
    if p.op == "in":
        return c.is_in(list(p.value))
    if p.op == "is_null":
        return c.is_null()
    if p.op == "is_not_null":
        return c.is_not_null()
    return _OPS[p.op](c, pl.lit(p.value))


def _select_sample(spec: SeedSpec, kf: pl.DataFrame, job_seed: bytes) -> tuple[frozenset[int], int]:
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
                code="subset_seed_key_uncanonicalizable",
                message=f"table {spec.table!r}: seed key column value could not be "
                f"canonicalized for sampling: {exc}",
            ) from exc
        digests.append((ctx.derive_source(ns, source), ri))
    digests.sort()
    selected = frozenset(ri for _, ri in digests[:k])
    return selected, seed_null_excluded


def _select_filter(spec: SeedSpec, kf: pl.DataFrame) -> frozenset[int]:
    expr = reduce(operator.and_, (_predicate_expr(p) for p in spec.predicates))
    return frozenset(kf.filter(expr)[RI].to_list())


def _select_keys(spec: SeedSpec, kf: pl.DataFrame) -> frozenset[int]:
    try:
        key_df = pl.DataFrame(list(spec.keys), schema=list(spec.key_columns), orient="row")
        key_df = key_df.select([pl.col(c).cast(kf.schema[c]) for c in spec.key_columns])
    except (pl.exceptions.PolarsError, TypeError, ValueError) as exc:
        raise SubsetConfigError(
            code="subset_seed_key_type",
            message=f"table {spec.table!r}: explicit seed key(s) do not match the key "
            f"column dtypes: {exc}",
        ) from exc
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
    seen_tables: set[str] = set()
    seed_rows: dict[str, frozenset[int]] = {}
    seed_null_excluded: dict[str, int] = {}
    for spec in seeds:
        if spec.table in seen_tables:
            raise SubsetConfigError(
                code="subset_duplicate_seed_table",
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

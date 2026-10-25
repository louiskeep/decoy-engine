"""Per-batch oracle-handler bridge for own-column row-erroring strategies on
the out-of-core route (F2, the row-error channel).

`bucketize` and `top_code` are row-local in VALUE but emit per-value
`format_error` RowErrors (an uncoercible numeric cell) that the pure-Arrow
out-of-core kernels have no channel to record. Rather than re-port their
coercion plus the subtle chunk-boundary canonical-rendering their handlers
already solved (`_strategies/_top_code.py`, `_strategies/_bucketize.py`) to a
new Arrow kernel, this bridge runs the SAME full-frame pandas handler on each
bounded record batch. That is byte-identical to the pandas oracle by
construction, the way the chunked route already runs the oracle per chunk
(`_chunked_oracle._oracle_masked`), and a batch is bounded by `batch_rows`, so
peak residency stays bounded.

It collects the handler's RowErrors with an ABSOLUTE row index (the batch's
durable Phase-1 source offset plus the handler's batch-local index, so the
reported position matches the whole-frame oracle, not a batch-local one) and
returns them for the runner to fail closed. Neither non-full-frame lane
quarantines row errors; both fail closed with `RowErrorsFailedError` the moment
any slice reports one (the routing policy, `_pipeline_routing.py`; the chunked
H1 check, `_chunked_oracle.py`). The out-of-core route follows the same policy.

Scope: OWN-COLUMN strategies only. bucketize and top_code read only their own
column, so a single-column batch slice reproduces the oracle with no
cross-column masking order to preserve. Sibling-reading strategies
(windowed_date's anchor, group_key's group_by) are not handled here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pyarrow as pa

if TYPE_CHECKING:
    from collections.abc import Sequence

from decoy_engine.execution._adapter import StrategyContext
from decoy_engine.execution._fk_keys import to_pandas_fk_safe
from decoy_engine.execution._row_errors import RowErrorRecord
from decoy_engine.execution._strategies import SCALAR_HANDLERS
from decoy_engine.execution.out_of_core._source import LazySource
from decoy_engine.generation.pool import PoolCache
from decoy_engine.relationships import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

if TYPE_CHECKING:
    from typing import Any

    from decoy_engine.plan._types import ColumnSeed, TableSeed
    from decoy_engine.providers_v2 import ProviderRegistry

# Own-column row-erroring strategies: value row-local, emit `format_error`, read
# no sibling column. Payload only; the compat gate keeps them off the FK
# parent-key surface (`_INITIAL_SUPPORTED_STRATEGIES`).
OWN_COLUMN_ROW_ERROR_STRATEGIES = frozenset({"bucketize", "top_code"})

# Strategies whose column the adapter reads through the lossless nullable-int
# path (`_pandas_adapter.run` unions `top_code_columns` into its FK-safe set so
# a value >= 2**53 is not float-rounded before bucketing). bucketize is NOT in
# that set there, so it takes the plain pyarrow default here too; matching the
# adapter's per-strategy conversion choice is what keeps this byte-identical.
_LOSSLESS_BRIDGE_STRATEGIES = frozenset({"top_code"})

_EMPTY_GRAPH = RelationshipGraph(edges=(), ordering=())
_EMPTY_NS = NamespaceRegistry(bindings=())


def _representative_pandas_dtype(arrow_type: pa.DataType, *, has_null: bool, fk_safe: bool) -> Any:
    """The pandas dtype `to_pandas_fk_safe` yields for a WHOLE column of
    `arrow_type` with or without nulls.

    Derived from a one-element representative array (all-null for the has-null
    case, empty for the no-null case) converted through the SAME conversion the
    oracle and the per-batch bridge both use, so it reproduces the oracle's
    whole-frame dtype exactly without reimplementing pyarrow's null-widening
    rules and without materializing the real column. An all-null array yields
    the same dtype pyarrow assigns any null-bearing column of the type (object
    for bool, float64 for a non-lossless int, ...); an empty array yields the
    null-free dtype.
    """
    rep = pa.array([None], type=arrow_type) if has_null else pa.array([], type=arrow_type)
    df = to_pandas_fk_safe(pa.table({"_": rep}), {"_"} if fk_safe else set())
    return df["_"].dtype


def _bridge_column_dtype_pair(arrow_type: pa.DataType, strategy: str) -> tuple[Any, Any]:
    """The (no-null, has-null) whole-column pandas dtypes for a bridge column.

    pyarrow's `to_pandas` is null-content dependent for some Arrow types (a
    boolean column is numpy `bool` with no nulls but `object` with any null; a
    non-lossless integer is `int64` vs `float64`), so a bounded batch that
    happens to be null-free infers a different dtype -- and a different top_code
    handler branch -- than the oracle's whole-column conversion. When the pair
    differs the column is null-UNSTABLE and its batches must be forced to the
    whole-column dtype; when it is equal no forcing is needed and no null scan is
    paid.
    """
    fk_safe = strategy in _LOSSLESS_BRIDGE_STRATEGIES
    return (
        _representative_pandas_dtype(arrow_type, has_null=False, fk_safe=fk_safe),
        _representative_pandas_dtype(arrow_type, has_null=True, fk_safe=fk_safe),
    )


def _columns_with_null(raw: Any, columns: list[str], batch_rows: int) -> set[str]:
    """The subset of `columns` whose WHOLE source column holds any null, read as
    cheaply as the source allows: O(1) metadata for a resident table, Parquet
    footer statistics for a `LazySource`, and a SINGLE bounded batch scan (not one
    per column) only for the columns whose footer has no null-count statistic
    (statistics written off), stopping as soon as all are decided."""
    if not isinstance(raw, LazySource):
        return {c for c in columns if _resident_has_null(raw, c)}
    footer = raw.column_null_counts()
    result: set[str] = set()
    unknown: list[str] = []
    for column in columns:
        count = footer.get(column)
        if count is None:
            unknown.append(column)
        elif count > 0:
            result.add(column)
    if unknown:
        for batch in raw.iter_batches(batch_rows):
            pending = [c for c in unknown if c not in result]
            if not pending:
                break
            for column in pending:
                idx = batch.schema.get_field_index(column)
                if idx >= 0 and batch.column(idx).null_count > 0:
                    result.add(column)
    return result


def _resident_has_null(raw: Any, column: str) -> bool:
    idx = raw.schema.get_field_index(column)
    return idx >= 0 and raw.column(idx).null_count > 0


def bridge_column_dtypes_for_table(
    raw: Any,
    source_schema: pa.Schema,
    table_column_seed: TableSeed | None,
    skip_columns: frozenset[str],
    batch_rows: int,
) -> dict[str, Any]:
    """Map each own-column row-erroring column of a table to the pandas dtype its
    batches must be forced to for whole-frame parity; a column absent from the
    map needs no forcing (its dtype is null-stable). Computed ONCE per table,
    before the batch stream, so every batch converts consistently (F2).

    Only null-UNSTABLE columns are resolved, and their whole-column null presence
    is read in ONE pass over the source (or from footer stats), so a stable
    column costs no scan and N unstable columns cost one scan, not N."""
    if table_column_seed is None:
        return {}
    unstable: dict[str, tuple[Any, Any]] = {}
    for column, seed in table_column_seed.per_column:
        if column in skip_columns or seed.strategy not in OWN_COLUMN_ROW_ERROR_STRATEGIES:
            continue
        idx = source_schema.get_field_index(column)
        if idx < 0:
            continue
        no_null_dtype, has_null_dtype = _bridge_column_dtype_pair(
            source_schema.field(idx).type, seed.strategy
        )
        if no_null_dtype != has_null_dtype:
            unstable[column] = (no_null_dtype, has_null_dtype)
    if not unstable:
        return {}
    has_null = _columns_with_null(raw, list(unstable), batch_rows)
    return {
        column: (has_null_dtype if column in has_null else no_null_dtype)
        for column, (no_null_dtype, has_null_dtype) in unstable.items()
    }


def bridge_own_column(
    table: str,
    column: str,
    seed: ColumnSeed,
    source_array: pa.Array | pa.ChunkedArray,
    *,
    registry: ProviderRegistry,
    job_seed: bytes,
    mask_key: bytes,
    row_offset: int,
    column_pandas_dtype: Any | None = None,
) -> tuple[pa.Array, tuple[RowErrorRecord, ...]]:
    """Mask one own-column row-erroring column of a batch via its oracle handler.

    Returns the masked array with the type `pa.Table.from_pandas` NATURALLY
    infers (string for a value-bearing batch, null-typed for an all-null or
    empty one), exactly as the full-frame oracle and the out-of-core `redact`
    kernel do: the fixed-schema sink reconciles a null-typed batch to the
    analytic type (`_emit.reconcile_batch`), and the resident path narrows
    per-batch types (`_join`), so NOT casting the OUTPUT here keeps the
    empty/all-null column byte-identical to the oracle's own inference. RowErrors
    come back as absolute-indexed `RowErrorRecord`s (`row_offset` + the handler's
    batch-local index), so a fail-closed report names the whole-frame position.

    `column_pandas_dtype` (F2), when given, is the pandas dtype the whole source
    column converts to under the oracle. The per-batch Arrow->pandas conversion
    is null-content dependent for some types, so a null-free batch of a
    null-bearing column would otherwise infer a different dtype (and a different
    handler branch) than the oracle's whole-column conversion; forcing the INPUT
    column to this dtype makes every batch take the same branch as the oracle.
    None means the column's dtype is null-stable and needs no forcing.
    """
    one_col = pa.table({column: source_array})
    fk_safe = {column} if seed.strategy in _LOSSLESS_BRIDGE_STRATEGIES else set()
    df = to_pandas_fk_safe(one_col, fk_safe)
    if column_pandas_dtype is not None:
        df[column] = df[column].astype(column_pandas_dtype)
    ctx = StrategyContext(
        registry=registry,
        pool_cache=PoolCache(),
        relationship_graph=_EMPTY_GRAPH,
        namespace_registry=_EMPTY_NS,
        job_seed=job_seed,
        mask_key=mask_key,
        row_offset=row_offset,
        current_table=table,
    )
    out_df, _warnings = SCALAR_HANDLERS[seed.strategy].run(df, column, seed, ctx)
    masked = pa.Table.from_pandas(out_df[[column]], preserve_index=False).column(0).combine_chunks()
    records = tuple(
        RowErrorRecord(
            table=table,
            column=e.column,
            row_index=e.row_index + row_offset,
            trigger=e.trigger,
            reason=e.reason,
        )
        for e in ctx.row_errors
    )
    return masked, records


def fail_closed_on_row_errors(records: Sequence[RowErrorRecord]) -> None:
    """Raise `RowErrorsFailedError` when a batch produced any row errors.

    The out-of-core route fails closed on the first batch with a row error, the
    same H1 contract the chunked route enforces per chunk (`_chunked_oracle`):
    neither non-full-frame lane quarantines row errors (`_pipeline_routing`), so
    a format error that cannot be routed anywhere fails the job loud rather than
    silently keep the raw source value. The records already carry absolute
    (whole-frame) row indices, so the error message names the true positions.
    """
    if records:
        from decoy_engine.errors import RowErrorsFailedError

        raise RowErrorsFailedError(tuple(records))


__all__ = [
    "OWN_COLUMN_ROW_ERROR_STRATEGIES",
    "bridge_column_dtypes_for_table",
    "bridge_own_column",
    "fail_closed_on_row_errors",
]

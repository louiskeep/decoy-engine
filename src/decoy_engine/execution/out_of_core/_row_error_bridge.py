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
from decoy_engine.generation.pool import PoolCache
from decoy_engine.relationships import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

if TYPE_CHECKING:
    from decoy_engine.plan._types import ColumnSeed
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
) -> tuple[pa.Array, tuple[RowErrorRecord, ...]]:
    """Mask one own-column row-erroring column of a batch via its oracle handler.

    Returns the masked array with the type `pa.Table.from_pandas` NATURALLY
    infers (string for a value-bearing batch, null-typed for an all-null or
    empty one), exactly as the full-frame oracle and the out-of-core `redact`
    kernel do: the fixed-schema sink reconciles a null-typed batch to the
    analytic type (`_emit.reconcile_batch`), and the resident path narrows
    per-batch types (`_join`), so NOT casting here keeps the empty/all-null
    column byte-identical to the oracle's own inference. RowErrors come back as
    absolute-indexed `RowErrorRecord`s (`row_offset` + the handler's batch-local
    index), so a fail-closed report names the whole-frame position.
    """
    one_col = pa.table({column: source_array})
    fk_safe = {column} if seed.strategy in _LOSSLESS_BRIDGE_STRATEGIES else set()
    df = to_pandas_fk_safe(one_col, fk_safe)
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
    "bridge_own_column",
    "fail_closed_on_row_errors",
]

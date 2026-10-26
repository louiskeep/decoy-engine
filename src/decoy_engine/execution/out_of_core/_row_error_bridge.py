"""Per-batch oracle-handler bridge for own-column row-erroring strategies on
the out-of-core route (F2, the row-error channel).

`bucketize` and `top_code` are row-local in VALUE but emit per-value
`format_error` RowErrors (an uncoercible numeric cell) that the pure-Arrow
out-of-core kernels have no channel to record. Rather than re-port their
coercion plus the subtle chunk-boundary canonical-rendering their handlers
already solved (`_strategies/_top_code.py`, `_strategies/_bucketize.py`) to a
new Arrow kernel, this bridge runs the SAME full-frame pandas handler on each
bounded record batch. That is byte-identical to the pandas oracle ONLY when a
batch's Arrow->pandas conversion reproduces the oracle's whole-frame conversion,
which is not true for every column: a null-free sub-batch of a null-bearing
plain int column infers numpy int64 where the whole column widens to float64,
and a pandas-metadata-bearing extension column (nullable boolean/Int64/...)
restores its extension dtype whole-frame but drops to object/float64 per bare
batch. Forcing each batch to the oracle's whole-frame dtype was tried and
abandoned (the Arrow->pandas dtype surface is too large to prove parity over);
instead the compat gate ADMITS a column on this bridge only when its batched
conversion is PROVABLY identical to the oracle's for every batch split
(`row_error_bridge_batch_stable`), and DECLINES the rest to the full-frame
oracle (`_compat.check_out_of_core_compatibility`,
`out_of_core_row_error_dtype_unsupported`). So an admitted column needs no
forcing: its bare per-batch conversion already matches the oracle. A batch is
bounded by `batch_rows`, so peak residency stays bounded.

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
    from typing import Any

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

# The Arrow types whose pandas conversion carries no batch-content-dependent
# state: a plain (non-dictionary) numpy integer/floating column, or a string
# column (-> object). A dictionary/categorical column's categories depend on the
# values present in a batch, a decimal/temporal/binary column is not a value we
# admit, and a plain boolean column null-widens; all fall outside this tuple and
# are declined. Integer/floating null-widening is still caught by the stability
# check below, so this allowlist is necessary but not sufficient on its own.
_BATCH_STABLE_ARROW_PREDICATES = (
    pa.types.is_integer,
    pa.types.is_floating,
    pa.types.is_string,
    pa.types.is_large_string,
)

_EMPTY_GRAPH = RelationshipGraph(edges=(), ordering=())
_EMPTY_NS = NamespaceRegistry(bindings=())


def _representative_pandas_dtype(
    arrow_type: pa.DataType, column: str, *, has_null: bool, metadata: Any, fk_safe: bool
) -> Any:
    """The pandas dtype a column of `arrow_type` named `column` converts to
    through the SAME `to_pandas_fk_safe` conversion the oracle and bridge use.

    Value-free: built from a one-element representative (all-null for the
    has-null case, empty otherwise), so it reproduces pyarrow's null-widening and
    (when `metadata` carries the source schema's `pandas` block) its
    extension-dtype restoration WITHOUT reimplementing either and without
    materializing the real column. `metadata=None` is the bare Arrow default (no
    pandas metadata) a per-batch array carries; the source schema metadata is the
    metadata-aware path the whole-frame oracle takes. `fk_safe` selects the
    lossless nullable-int `types_mapper` (top_code) vs the plain default
    (bucketize), matching each strategy's own conversion.
    """
    rep = pa.array([None], type=arrow_type) if has_null else pa.array([], type=arrow_type)
    rep_table = pa.table({column: rep})
    if metadata is not None:
        rep_table = rep_table.replace_schema_metadata(metadata)
    return to_pandas_fk_safe(rep_table, {column} if fk_safe else set())[column].dtype


def row_error_bridge_batch_stable(source_schema: pa.Schema, column: str, strategy: str) -> bool:
    """True when `column`'s per-batch Arrow->pandas conversion on this bridge is
    PROVABLY identical to the oracle's whole-frame conversion for every possible
    batch split; False means DECLINE the OOC route for the job (run the oracle).

    Three conditions, each value-free (a function of the Arrow type + the source
    schema's pandas metadata, never the data):

    1. The Arrow type is a plain numpy integer/floating or a string type (no
       dictionary/categorical, decimal, temporal, binary, or plain boolean). A
       dictionary column's categories are batch-content-dependent; the others are
       not a value this bridge admits.
    2. No load-bearing pandas-extension metadata: the metadata-aware conversion
       (what the oracle sees) equals the bare conversion (what a per-batch array
       sees) for both null states. A nullable `boolean`/`Int64`/`Float64`/string
       extension source restores its extension dtype whole-frame but not per bare
       batch, so it is declined even when a strategy's fk-safe `types_mapper`
       would later normalize it -- compared on the PLAIN path (`fk_safe=False`)
       so the metadata signal is read directly, not masked by the mapper.
    3. Null-stable under the strategy's own conversion: a null-free batch and a
       null-bearing batch infer the same dtype, so no batch boundary moves a cell
       into a different handler branch. A plain int/uint column widens to float64
       under the plain (bucketize) path but stays its own nullable extension
       dtype under the lossless (top_code) path, so this is strategy-aware.
    """
    arrow_type = source_schema.field(column).type
    if not any(predicate(arrow_type) for predicate in _BATCH_STABLE_ARROW_PREDICATES):
        return False
    metadata = source_schema.metadata
    for has_null in (False, True):
        bare = _representative_pandas_dtype(
            arrow_type, column, has_null=has_null, metadata=None, fk_safe=False
        )
        meta = _representative_pandas_dtype(
            arrow_type, column, has_null=has_null, metadata=metadata, fk_safe=False
        )
        if bare != meta:
            return False
    fk_safe = strategy in _LOSSLESS_BRIDGE_STRATEGIES
    no_null = _representative_pandas_dtype(
        arrow_type, column, has_null=False, metadata=None, fk_safe=fk_safe
    )
    has_null = _representative_pandas_dtype(
        arrow_type, column, has_null=True, metadata=None, fk_safe=fk_safe
    )
    return no_null == has_null


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
    per-batch types (`_join`), so NOT casting the OUTPUT here keeps the
    empty/all-null column byte-identical to the oracle's own inference. RowErrors
    come back as absolute-indexed `RowErrorRecord`s (`row_offset` + the handler's
    batch-local index), so a fail-closed report names the whole-frame position.

    The per-batch Arrow->pandas conversion here starts from a bare single-column
    Arrow table that carries no pandas metadata. That is byte-identical to the
    oracle's whole-frame conversion only for a batch-stable column, which is the
    only shape the compat gate admits onto this bridge
    (`row_error_bridge_batch_stable`); a metadata-bearing extension column or a
    null-unstable plain column is DECLINED to the full-frame oracle before it
    reaches here, so no per-batch dtype forcing is needed.
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
    "row_error_bridge_batch_stable",
]

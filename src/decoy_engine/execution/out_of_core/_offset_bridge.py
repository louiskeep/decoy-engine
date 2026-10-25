"""Per-batch oracle-handler bridge for the out-of-core row-offset channel (F3).

`windowed_date` is position-keyed: each row's output date is a pure function of
its seed-derived offset keyed on the row's GLOBAL position in the whole,
unchunked source (`transforms.windowed_date`, the chunked route's DGRN). The
out-of-core route now threads that durable global row ordinal into `mask_batch`
(the cumulative Phase-1 source-order row count; masking runs before any join or
reorder, so it equals the whole-source position), so windowed_date can run here
through the SAME full-frame handler on each bounded batch, byte-identical to the
oracle, with `ctx.row_offset` set to the batch's ordinal -- the way the chunked
route runs the oracle per chunk with `base_row_offset`.

windowed_date also reads a sibling ANCHOR column. The compat gate admits it only
when that anchor is unmasked or `passthrough` (`_compat._windowed_date_rejection`),
so the RAW anchor this bridge reads from the batch equals the value the oracle
reads -- no cross-column masking order to reproduce. A non-null anchor that fails
the grammar/range raises `WindowedDateError` on BOTH routes (fail-closed, no row
error), so this bridge needs no row-error channel.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pyarrow as pa

from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._fk_keys import to_pandas_fk_safe
from decoy_engine.execution._strategies import SCALAR_HANDLERS
from decoy_engine.generation.pool import PoolCache
from decoy_engine.relationships import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

if TYPE_CHECKING:
    from decoy_engine.plan._types import ColumnSeed
    from decoy_engine.providers_v2 import ProviderRegistry

# Position-keyed strategies the row-offset channel admits. Payload only; the FK
# parent-key surface stays `_INITIAL_SUPPORTED_STRATEGIES`, so an offset-keyed FK
# key is a MISS (and would break RI: a child's chunk-relative position differs
# from its parent's, `_chunked_dgrn.py`).
OFFSET_BRIDGE_STRATEGIES = frozenset({"windowed_date"})

_EMPTY_GRAPH = RelationshipGraph(edges=(), ordering=())
_EMPTY_NS = NamespaceRegistry(bindings=())


def windowed_date_anchor(seed: ColumnSeed) -> str | None:
    """The anchor column name a windowed_date seed references, or None."""
    anchor = provider_config_to_dict(seed.provider_config).get("anchor")
    return str(anchor) if anchor else None


def bridge_windowed_date(
    table: str,
    column: str,
    seed: ColumnSeed,
    anchor_name: str,
    anchor_array: pa.Array | pa.ChunkedArray,
    *,
    registry: ProviderRegistry | None,
    job_seed: bytes,
    mask_key: bytes,
    row_offset: int,
) -> pa.Array:
    """Mask one windowed_date column of a batch via its oracle handler.

    `row_offset` is the batch's durable global row ordinal, so row `j` of the
    batch keys on global position `row_offset + j`, matching the full-frame
    oracle's whole-source enumeration. The anchor is converted through the plain
    pyarrow default (the oracle does not union a windowed_date anchor into its
    FK-safe lossless set), so the conversion matches.
    """
    if registry is None:
        raise ExecutionError(
            code="out_of_core_windowed_date_registry_missing",
            message=(
                f"{table}.{column}: out-of-core windowed_date needs a provider registry "
                "for its handler bridge, but none was threaded."
            ),
        )
    df = to_pandas_fk_safe(pa.table({anchor_name: anchor_array}), set())
    # The handler reads df[anchor] and WRITES df[column]; the target column is
    # not in the input frame (and its prior value is never read), so a null
    # placeholder is enough for the handler to overwrite.
    df[column] = None
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
    out_df, _warnings = SCALAR_HANDLERS["windowed_date"].run(df, column, seed, ctx)
    return pa.Table.from_pandas(out_df[[column]], preserve_index=False).column(0).combine_chunks()


__all__ = ["OFFSET_BRIDGE_STRATEGIES", "bridge_windowed_date", "windowed_date_anchor"]

"""Out-of-core ingest guard: reject an integer column with nulls under truncate/categorical.

Same rule and same error as `execution._guards.reject_null_bearing_int`, but the
out-of-core sources are lazy Parquet readers that must not be materialized, so
the null status comes from footer counts (`LazySource.column_null_counts`). A
column whose footer count is unknown (statistics written off, or any nonempty
row group missing them) is counted by streaming only that column until the first
null or EOF: assuming "no nulls" would keep the fail-open bug and rejecting on
unknown statistics would refuse clean jobs, and a bounded prefix would miss a
late null.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.execution._guards import null_bearing_int_candidates, null_bearing_int_error
from decoy_engine.execution._runner import WorkNode
from decoy_engine.profile._readers import LazySource
from decoy_engine.relationships import RelationshipGraph

# Matches the runner's pinned streaming batch size; only bounds the scan's peak memory.
_SCAN_BATCH_ROWS = 65_536


def _lazy_has_null(source: LazySource, column: str, footer: dict[str, int | None]) -> bool:
    known = footer.get(column)
    if known is not None:
        return known > 0
    parquet_file = pq.ParquetFile(source.path)
    try:
        for batch in parquet_file.iter_batches(batch_size=_SCAN_BATCH_ROWS, columns=[column]):
            if batch.column(0).null_count > 0:
                return True
    finally:
        parquet_file.close()
    return False


def reject_null_bearing_int_ooc(
    work: Iterable[WorkNode],
    sources: Mapping[str, pa.Table | LazySource],
    relationship_graph: RelationshipGraph,
) -> None:
    """Raise `null_bearing_int_unsupported` for a masked integer column that holds a null."""
    footers: dict[str, dict[str, int | None]] = {}
    for node in null_bearing_int_candidates(work, relationship_graph):
        source = sources.get(node.table)
        if source is None:
            continue
        column = node.columns[0]
        schema = source.schema
        if column not in schema.names or not pa.types.is_integer(schema.field(column).type):
            continue
        if isinstance(source, LazySource):
            if node.table not in footers:  # read each table's footer once, not per candidate
                footers[node.table] = source.column_null_counts()
            has_null = _lazy_has_null(source, column, footers[node.table])
        else:
            has_null = source.column(column).null_count > 0
        if has_null:
            raise null_bearing_int_error(node)

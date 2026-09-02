"""Drive tables through `_stream_driver.stream_table` in topological order.

Task 6 builds the reorder driver standalone; wiring it into a route (a
`table_name -> stream_table` loop that a caller reaches through
`run_fk_out_of_core`) is Task 7. This harness IS that loop, for tests only: it
mirrors `_runner.run_fk_out_of_core`'s outer topological walk
(`_edge_indexes` / `_table_order`, both reused unmodified) but calls
`stream_table` instead of `_runner._stream_table`, so multi-table fixtures
(chains, fanouts) can drive the reorder path exactly the way the eventual
route seam will, without that seam existing yet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution.out_of_core._runner import _edge_indexes, _table_order
from decoy_engine.execution.out_of_core._stream_driver import stream_table

if TYPE_CHECKING:
    from pathlib import Path

    from decoy_engine.execution._transactional_sink import TransactionalSink
    from decoy_engine.execution.out_of_core._relation import ParentKeyRelation
    from decoy_engine.execution.out_of_core._source import LazySource
    from decoy_engine.generation.pool._events import QualityWarning
    from decoy_engine.relationships._graph import RelationshipEdge, RelationshipGraph

_DEFAULT_RUN_BYTES_CAP = 8 * 1024 * 1024
_DEFAULT_MERGE_FAN_IN = 4


def run_stream_driver(
    plan: Any,
    sources: dict[str, pa.Table | LazySource],
    graph: RelationshipGraph,
    *,
    temp_dir: Path,
    sink: TransactionalSink | None = None,
    batch_rows: int | None = None,
    run_bytes_cap: int = _DEFAULT_RUN_BYTES_CAP,
    merge_fan_in: int = _DEFAULT_MERGE_FAN_IN,
    mask_key: bytes | None = None,
) -> ExecutionResult:
    """Run every table in `sources` through the reorder driver, topo-ordered."""
    incoming, outgoing = _edge_indexes(graph)
    parent_relations: dict[RelationshipEdge, ParentKeyRelation] = {}
    outputs: dict[str, pa.Table] = {}
    warnings: list[QualityWarning] = []
    root = temp_dir
    root.mkdir(parents=True, exist_ok=True)
    for table_name in _table_order(plan, graph, sources):
        if table_name not in sources:
            continue
        stream_table(
            plan,
            table_name,
            sources[table_name],
            incoming_edges=tuple(incoming[table_name]),
            outgoing_edges=tuple(outgoing[table_name]),
            parent_relations=parent_relations,
            temp_dir=root / "joins" / table_name,
            relation_dir=root / "relations" / table_name,
            staging_path=root / "staged" / table_name / "masked_keys.parquet",
            memory_limit=None,
            batch_rows=batch_rows,
            run_bytes_cap=run_bytes_cap,
            merge_fan_in=merge_fan_in,
            sink=sink,
            outputs=outputs,
            warnings=warnings,
            mask_key=mask_key,
        )
    if sink is not None:
        sink.commit()
        return ExecutionResult(outputs={}, warnings=tuple(warnings))
    return ExecutionResult(outputs=outputs, warnings=tuple(warnings))


__all__ = ["run_stream_driver"]

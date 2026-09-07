"""Drive tables through `_stream_driver.stream_table` in topological order.

Task 6 built the reorder driver standalone; Task 7 wired it into
`run_fk_out_of_core`'s live route selection (`_route_policy.decide_route`).
This harness predates that seam and still mirrors `run_fk_out_of_core`'s
outer topological walk directly (`_edge_indexes` / `_table_order`, both
reused unmodified from their new home in `_route_policy.py`) but calls
`stream_table` unconditionally instead of routing through `decide_route`,
so multi-table fixtures (chains, fanouts) can drive the reorder path on
every table regardless of parent-key size or threshold.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution.out_of_core._route_policy import _edge_indexes, _table_order
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
    code_set_corpora: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> ExecutionResult:
    """Run every table in `sources` through the reorder driver, topo-ordered.

    `code_set_corpora` (default None): passed straight through to every
    `stream_table` call, exactly as `_runner.run_fk_out_of_core` threads its
    own same-named dict -- a caller-owned sink so a multi-table run's evidence
    (and the withheld-stamp cases) can be inspected after the call, the same
    shape `run_fk_out_of_core` folds into `ExecutionResult.quality_metrics`.
    """
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
            code_set_corpora=code_set_corpora,
        )
    quality_metrics: dict[str, Any] = (
        {"code_set_corpora": list(code_set_corpora.values())} if code_set_corpora else {}
    )
    if sink is not None:
        sink.commit()
        return ExecutionResult(
            outputs={}, warnings=tuple(warnings), quality_metrics=quality_metrics
        )
    return ExecutionResult(
        outputs=outputs, warnings=tuple(warnings), quality_metrics=quality_metrics
    )


__all__ = ["run_stream_driver"]

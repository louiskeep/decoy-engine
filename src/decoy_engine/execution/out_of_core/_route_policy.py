"""Sink-path route selection for the out-of-core FK runner: choose between
the reorder driver (`_stream_driver.stream_table`) and the existing
`_batch_join` route (`_runner._stream_table`) per table, by parent-key size.

`_table_order` / `_edge_indexes` (the topology helpers `_runner.py`'s outer
loop also needs) moved here from `_runner.py` alongside the new dispatch
logic, freeing enough LOC to keep that module at its sentry ceiling
(`tests/sentry/test_module_size.py`) while it gains the per-table route
call this seam adds.

Imports only relation metadata (`pyarrow.parquet`), `_reorder_budget`, and
`_memory_estimate` -- never `_runner` / `_stream_driver` -- so the
dependency stays strictly one-way: this module can be imported before
either of those exists in scope, with no import-cycle risk.
"""

from __future__ import annotations

import heapq
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import pyarrow.parquet as pq

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.out_of_core._memory_estimate import memory_limit_for
from decoy_engine.execution.out_of_core._reorder_budget import (
    ReorderCaps,
    resolve_reorder_budgets,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    import pyarrow as pa

    from decoy_engine.execution._transactional_sink import TransactionalSink
    from decoy_engine.execution.out_of_core._relation import ParentKeyRelation
    from decoy_engine.execution.out_of_core._source import LazySource
    from decoy_engine.plan._types import Plan
    from decoy_engine.relationships import RelationshipGraph
    from decoy_engine.relationships._graph import RelationshipEdge

# Default parent-key-count boundary above which a sink-path table with a
# resolvable memory + disk budget auto-selects the reorder route.
# Overridable per run via `out_of_core_reorder_threshold_rows`
# (`resolve_reorder_threshold_rows`).
REORDER_PARENT_KEY_THRESHOLD: Final = 2_000_000

# Matches `_stream_driver.stream_table`'s own default and `resolve_reorder_
# budgets`'s. Checked BEFORE a `ReorderBudgets` exists (the high-fan-in
# fallback below is a pure topology check, not a budget one), so it is
# duplicated here as a plain constant rather than threaded from a budget
# object that has not been resolved yet at that point.
_MERGE_FAN_IN_DEFAULT: Final = 16


@dataclass(frozen=True)
class RouteDecision:
    """One table's route choice: `_stream_table` (`_batch_join`) unless
    `use_reorder`, in which case `reorder_caps` sizes the reorder driver."""

    use_reorder: bool
    reorder_caps: ReorderCaps | None


def resolve_reorder_threshold_rows(value: int | None) -> int:
    """Validate + resolve the `out_of_core_reorder_threshold_rows` override.

    `None` resolves to the default threshold. `0` is a valid override
    meaning "reorder every eligible sink table" (forcing the route for
    calibration/tests), not "never reorder" -- treating it as false-y would
    invert the override's own documented semantics. A `bool` (Python's
    `isinstance(True, int)` is `True`, the common `True`/`1` mix-up) and a
    negative value are both genuinely invalid and raise rather than being
    silently coerced.
    """
    if value is None:
        return REORDER_PARENT_KEY_THRESHOLD
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExecutionError(
            code="out_of_core_reorder_threshold_invalid",
            message=(f"out_of_core_reorder_threshold_rows must be an int or None, got {value!r}."),
        )
    if value < 0:
        raise ExecutionError(
            code="out_of_core_reorder_threshold_invalid",
            message=f"out_of_core_reorder_threshold_rows must be >= 0, got {value}.",
        )
    return value


def decide_route(
    incoming_edges: tuple[RelationshipEdge, ...],
    parent_relations: Mapping[RelationshipEdge, ParentKeyRelation],
    *,
    sink: TransactionalSink | None,
    budget_bytes: int | None,
    temp_disk_budget_bytes: int | None,
    threshold_rows: int,
    merge_fan_in: int = _MERGE_FAN_IN_DEFAULT,
) -> RouteDecision:
    """Choose `_batch_join` (default) or the reorder driver for one table.

    Sink-path only (`_stream_driver.stream_table`'s module docstring:
    bounded residency needs the sink + `LazySource` shape). Any false
    condition below keeps the current byte-for-byte `_batch_join` behavior,
    so a non-sink, budget-less, sub-threshold, or high-fan-in table never
    regresses or fails. High fan-in falls back rather than raising: more
    incoming edges than `2 * merge_fan_in` co-resident phase-3 heads can
    admit is a plausible schema shape (wide event / ERP / healthcare
    tables), not a misconfiguration (`_reorder_budget.phase3_head_fit`).

    Decision key: the largest incoming edge's DEDUPED parent-key count (the
    relation build already computed this; its parquet footer `num_rows` is
    read directly rather than re-derived), not raw parent row count -- a
    parent with many duplicate/null raw rows but few distinct keys is priced
    by what the join actually consumes.
    """
    if (
        sink is None
        or not incoming_edges
        or budget_bytes is None
        or temp_disk_budget_bytes is None
        or len(incoming_edges) > 2 * merge_fan_in
    ):
        return RouteDecision(use_reorder=False, reorder_caps=None)
    parent_key_count = max(
        (_parent_key_count(parent_relations[edge]) for edge in incoming_edges), default=0
    )
    if parent_key_count < threshold_rows:
        return RouteDecision(use_reorder=False, reorder_caps=None)
    budgets = resolve_reorder_budgets(
        budget_bytes, temp_disk_budget_bytes, merge_fan_in=merge_fan_in
    )
    reorder_caps = ReorderCaps(
        joiner_memory_limit=memory_limit_for(budgets.duckdb_memory_limit_bytes, 1),
        build_memory_limit=memory_limit_for(budget_bytes, 1),
        run_bytes_cap=budgets.run_bytes_cap,
        merge_fan_in=budgets.merge_fan_in,
    )
    # Width admission, PER EDGE: a slim sorter row wider than the sorter's
    # per-merge-head cap (`_external_sort.py`'s `out_of_core_sort_row_too_wide`)
    # would make the reorder route raise on a job `_batch_join` handles fine (no
    # per-row width limit there), so fall back rather than routing into a
    # guaranteed rejection -- fail SAFE toward `_batch_join`, same posture as the
    # high-fan-in fallback above. Each edge's phase-2 sort is SEQUENTIAL (one
    # connection at a time, `_stream_driver.py`), so compare each edge's bound
    # against the cap on its own -- never a cross-edge sum, which would price a
    # co-residency that never happens. `max_sort_payload_row_bytes` is a real
    # conservative bound on the materialized slim row, not an inferred proxy, so
    # no empirical width factor is applied.
    per_head_cap = reorder_caps.run_bytes_cap // (2 * reorder_caps.merge_fan_in)
    for edge in incoming_edges:
        if parent_relations[edge].max_sort_payload_row_bytes >= per_head_cap:
            return RouteDecision(use_reorder=False, reorder_caps=None)
    return RouteDecision(use_reorder=True, reorder_caps=reorder_caps)


def validate_outgoing_parent_columns(
    outgoing_edges: tuple[RelationshipEdge, ...],
    source_schema: pa.Schema,
) -> None:
    """Fail closed with the coded `out_of_core_parent_column_missing` (never a
    bare Arrow KeyError) when an outgoing edge names a parent-key column this
    table's schema lacks. Run route-independently BEFORE dispatch: the reorder
    driver dereferences these columns very early (building its raw-parent
    projection) and would otherwise raise an uncoded KeyError, while the batch
    route raises this coded error later in its relation build -- so the two
    routes must be pre-empted here to raise the SAME error at the SAME point.
    Message matches `_relation.py`'s so the two routes are byte-identical.
    """
    names = set(source_schema.names)
    for edge in outgoing_edges:
        for parent_col in edge.parent_columns:
            if parent_col not in names:
                raise ExecutionError(
                    code="out_of_core_parent_column_missing",
                    message=f"parent source table has no column {parent_col!r}.",
                )


def _parent_key_count(relation: ParentKeyRelation) -> int:
    """Distinct parent-key row count, off the relation's own parquet footer
    -- no data read, since this file already IS the deduped, null-filtered
    relation the join consumes. Kept here rather than as a `ParentKeyRelation`
    property: `_relation.py` is close enough to the 600-LOC module cap that a
    documented cached property would breach it, so the footer read lives here."""
    return pq.read_metadata(relation.path).num_rows


def _edge_indexes(
    relationship_graph: RelationshipGraph,
) -> tuple[dict[str, list[RelationshipEdge]], dict[str, list[RelationshipEdge]]]:
    incoming: dict[str, list[RelationshipEdge]] = defaultdict(list)
    outgoing: dict[str, list[RelationshipEdge]] = defaultdict(list)
    for edge in relationship_graph.edges:
        incoming[edge.child_table].append(edge)
        outgoing[edge.parent_table].append(edge)
    return incoming, outgoing


def _table_order(
    plan: Plan,
    relationship_graph: RelationshipGraph,
    sources: Mapping[str, pa.Table | LazySource],
) -> list[str]:
    tables = {table for table, _seed in plan.seed_envelope.per_table} | set(sources)
    deps: dict[str, set[str]] = {table: set() for table in tables}
    children: dict[str, set[str]] = defaultdict(set)
    for edge in relationship_graph.edges:
        tables.add(edge.parent_table)
        tables.add(edge.child_table)
        deps.setdefault(edge.parent_table, set())
        deps.setdefault(edge.child_table, set()).add(edge.parent_table)
        children[edge.parent_table].add(edge.child_table)
    ready = [table for table in tables if not deps.get(table)]
    heapq.heapify(ready)
    ordered: list[str] = []
    while ready:
        table = heapq.heappop(ready)
        ordered.append(table)
        for child in sorted(children[table]):
            deps[child].discard(table)
            if not deps[child]:
                heapq.heappush(ready, child)
    if len(ordered) != len(tables):
        raise ExecutionError(
            code="out_of_core_relationship_cycle",
            message="out-of-core route requires an acyclic table graph.",
        )
    return ordered


__all__ = [
    "REORDER_PARENT_KEY_THRESHOLD",
    "RouteDecision",
    "decide_route",
    "resolve_reorder_threshold_rows",
    "validate_outgoing_parent_columns",
]

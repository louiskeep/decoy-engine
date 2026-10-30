"""FK-topological table order for the sequential route (split out of `_sequential`).

`_sequential` re-exports `table_topo_order` under its historical import path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from decoy_engine import reasons
from decoy_engine.execution._errors import ExecutionError

if TYPE_CHECKING:
    from decoy_engine.plan._types import Plan
    from decoy_engine.relationships import RelationshipGraph


def table_topo_order(plan: Plan, graph: RelationshipGraph) -> list[str]:
    """FK-topological order over TABLES (every parent before its children), stable
    to the plan's table order where the graph leaves a choice. Raises on a cycle.
    Sequential eviction relies on a parent being fully masked, and its key map
    built, before any child table is loaded."""
    order_seed: list[str] = []
    seen: set[str] = set()
    for name, _ in plan.seed_envelope.per_table:
        if name not in seen:
            seen.add(name)
            order_seed.append(name)
    for edge in graph.edges:
        for tbl in (edge.parent_table, edge.child_table):
            if tbl not in seen:
                seen.add(tbl)
                order_seed.append(tbl)

    position = {t: i for i, t in enumerate(order_seed)}
    children: dict[str, set[str]] = {t: set() for t in order_seed}
    indegree: dict[str, int] = dict.fromkeys(order_seed, 0)
    for edge in graph.edges:
        if edge.parent_table == edge.child_table:
            continue  # self-FK masks within one table; no table-level ordering
        if edge.child_table not in children[edge.parent_table]:
            children[edge.parent_table].add(edge.child_table)
            indegree[edge.child_table] += 1

    ready = [t for t in order_seed if indegree[t] == 0]
    result: list[str] = []
    while ready:
        ready.sort(key=position.__getitem__)
        current = ready.pop(0)
        result.append(current)
        for child in sorted(children[current], key=position.__getitem__):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)

    if len(result) != len(order_seed):
        raise ExecutionError(
            code=reasons.runtime.RELATIONSHIP_CYCLE,
            message=(
                "FK relationship graph has a cycle across tables; cannot order "
                "tables for sequential masking."
            ),
        )
    return result

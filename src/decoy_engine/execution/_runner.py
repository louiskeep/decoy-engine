"""Work-list construction + execution ordering for the pandas adapter (S9).

The load-bearing core (S9 spec §6; the BLOCKER Dennis caught at spec review):

- `build_work_list` enumerates the columns to mask from
  `plan.seed_envelope.per_table`, NOT from `plan.ordering`. `plan.ordering`
  is built from FK edges only and is EMPTY for a no-FK single-table job, so
  iterating it as the work list would silently leave non-FK columns unmasked.
  The seed envelope is the authoritative set of maskable columns.
- `order_work` computes an execution order over the FULL work list: FK parents
  before children (from `relationship_graph.edges`), and each composite node
  before any FK child that reads one of its output columns (R17). Independent
  nodes fall in deterministic (table, columns) sorted order via a Kahn topo
  sort with a sorted tie-break (byte-stable across runs).

These are pure functions (no pandas, no pyarrow); the concrete
PandasExecutionAdapter (later slice) consumes them.
"""

from __future__ import annotations

import heapq
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.plan._types import ColumnSeed, GroupSeed

if TYPE_CHECKING:
    from decoy_engine.plan._types import Plan
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import RelationshipGraph

_NodeKey = tuple[str, tuple[str, ...]]


@dataclass(frozen=True)
class WorkNode:
    """One unit of masking work: a scalar column, a collapsed composite bundle,
    or a composite-FK group (one node, not per-column)."""

    table: str
    columns: tuple[str, ...]
    kind: str  # "scalar" | "composite" | "composite_fk_group"
    strategy: str
    provider: str | None  # None for scalar transforms (hash/redact/...); see ColumnSeed
    plan_slice: ColumnSeed | GroupSeed

    @property
    def key(self) -> _NodeKey:
        return (self.table, self.columns)


def build_work_list(plan: Plan, registry: ProviderRegistry) -> list[WorkNode]:
    """Enumerate every maskable unit from the seed envelope (S9 spec §6.1).

    Covers single-table no-FK jobs (which `plan.ordering` does not). Composite
    output columns collapse into one bundle node keyed on the sorted union of
    (column + its coherent_with); a composite is recognized by
    `registry.get_capabilities(provider).backend_type == "composite"`.
    """
    work: list[WorkNode] = []
    for table, table_seed in plan.seed_envelope.per_table:
        composite_groups: dict[_NodeKey, list[ColumnSeed]] = {}
        for col_name, col_seed in table_seed.per_column:
            # Scalar transform strategies (hash/redact/truncate/passthrough/...) carry
            # NO provider (None) and read their settings from provider_config; only
            # registry-bound providers can be composites, so guard the capability lookup
            # (null-guard before registry.has, which expects a provider string).
            caps = (
                registry.get_capabilities(col_seed.provider)
                if col_seed.provider and registry.has(col_seed.provider)
                else None
            )
            if caps is not None and caps.backend_type == "composite":
                group_cols = tuple(sorted({col_name, *col_seed.coherent_with}))
                composite_groups.setdefault((table, group_cols), []).append(col_seed)
            else:
                work.append(
                    WorkNode(
                        table=table,
                        columns=(col_name,),
                        kind="scalar",
                        strategy=col_seed.strategy,
                        provider=col_seed.provider,
                        plan_slice=col_seed,
                    )
                )
        for (group_table, group_cols), seeds in composite_groups.items():
            head = seeds[0]  # all members share the composite provider
            work.append(
                WorkNode(
                    table=group_table,
                    columns=group_cols,
                    kind="composite",
                    strategy="<composite>",
                    provider=head.provider,
                    plan_slice=head,
                )
            )
        for _canonical_key, group_seed in table_seed.per_group:
            work.append(
                WorkNode(
                    table=table,
                    columns=group_seed.coherent_columns,
                    kind="composite_fk_group",
                    strategy="<group>",
                    provider="<group>",
                    plan_slice=group_seed,
                )
            )
    return work


def order_work(work: list[WorkNode], relationship_graph: RelationshipGraph) -> list[WorkNode]:
    """Order the work list (S9 spec §6.2).

    Dependency edges come from `relationship_graph.edges` (FK parent -> child),
    plus a composite-node -> FK-child edge whenever the composite's output
    columns are a superset of an FK's parent_columns (R17: the composite writes
    the whole bundle before any child reads a masked parent column). Topo-sorted
    with a sorted tie-break for byte-stable ordering.
    """
    by_key: dict[_NodeKey, WorkNode] = {w.key: w for w in work}
    deps: dict[_NodeKey, set[_NodeKey]] = defaultdict(set)

    for edge in relationship_graph.edges:
        parent_k = (edge.parent_table, edge.parent_columns)
        child_k = (edge.child_table, edge.child_columns)
        if child_k not in by_key:
            continue
        if parent_k in by_key:
            deps[child_k].add(parent_k)
        else:
            # Composite-key FK: the parent tuple is not a single work node; its
            # columns mask as individual scalar nodes. The composite-FK child
            # node waits on each parent column node so the parent key mapping is
            # fully masked before the child resolves against it.
            for pcol in edge.parent_columns:
                pcol_k = (edge.parent_table, (pcol,))
                if pcol_k in by_key:
                    deps[child_k].add(pcol_k)

    # R17: a composite output column that is also a FK parent. The FK child waits
    # on the whole composite bundle node, not a per-column node.
    for node in work:
        if node.kind != "composite":
            continue
        node_cols = set(node.columns)
        for edge in relationship_graph.edges:
            if edge.parent_table == node.table and set(edge.parent_columns).issubset(node_cols):
                child_k = (edge.child_table, edge.child_columns)
                if child_k in by_key:
                    deps[child_k].add(node.key)

    return _kahn_sorted(by_key, deps)


def _kahn_sorted(
    by_key: dict[_NodeKey, WorkNode], deps: dict[_NodeKey, set[_NodeKey]]
) -> list[WorkNode]:
    """Deterministic topological sort: at each step take the sorted-smallest node
    whose dependencies are all placed. Total order, byte-stable across runs.

    QA-10 F9 (2026-06-01): heapq-based Kahn replaces the prior O(n^2)
    "scan all keys + sort the ready set" loop. Pre-fix every
    topological step did a full `sorted(k for k in keys if ...)`,
    yielding O(n^2 log n) total work. Post-fix maintains a min-heap
    of ready nodes + an indegree counter; each node is pushed/popped
    once for O((n+e) log n) total. Output is byte-identical to the
    prior implementation (both yield the lexicographically smallest
    topological order). Same fix shape as QA-8 F1 closure on the
    relationship-graph builder.
    """
    keys = set(by_key)
    if not keys:
        return []
    # Build reverse adjacency + indegree counter so each placement
    # only revisits the children of the just-placed node, not the
    # full key set.
    rev: dict[_NodeKey, set[_NodeKey]] = defaultdict(set)
    indegree: dict[_NodeKey, int] = {k: 0 for k in keys}
    for child, parents in deps.items():
        if child not in keys:
            continue
        for parent in parents:
            if parent not in keys:
                continue
            rev[parent].add(child)
            indegree[child] += 1
    ready_heap: list[_NodeKey] = [k for k in keys if indegree[k] == 0]
    heapq.heapify(ready_heap)
    placed: list[_NodeKey] = []
    while ready_heap:
        nxt = heapq.heappop(ready_heap)
        placed.append(nxt)
        for child in sorted(rev[nxt]):
            indegree[child] -= 1
            if indegree[child] == 0:
                heapq.heappush(ready_heap, child)
    if len(placed) < len(keys):
        unplaced = sorted(k for k in keys if indegree[k] > 0)
        raise ExecutionError(
            code="cyclic_work_ordering",
            message=f"cycle in work-node dependencies; unresolved: {unplaced!r}",
        )
    return [by_key[k] for k in placed]

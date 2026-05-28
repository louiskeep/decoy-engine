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
    provider: str
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
            caps = registry.get_capabilities(col_seed.provider)
            if caps.backend_type == "composite":
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
        if parent_k in by_key and child_k in by_key:
            deps[child_k].add(parent_k)

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
    whose dependencies are all placed. Total order, byte-stable across runs."""
    keys = set(by_key)
    placed: list[_NodeKey] = []
    placed_set: set[_NodeKey] = set()
    while len(placed) < len(keys):
        ready = sorted(k for k in keys if k not in placed_set and deps.get(k, set()) <= placed_set)
        if not ready:
            unplaced = sorted(keys - placed_set)
            raise ExecutionError(
                code="cyclic_work_ordering",
                message=f"cycle in work-node dependencies; unresolved: {unplaced!r}",
            )
        nxt = ready[0]
        placed.append(nxt)
        placed_set.add(nxt)
    return [by_key[k] for k in placed]

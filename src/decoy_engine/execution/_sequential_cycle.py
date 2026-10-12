"""Cross-table FK cycles on the sequential (SEQ) route: components, table order, the key pass.

The TABLE graph of an admitted cycle cycles, but its COLUMN graph does not: that is why the
full-frame path masks it, in column-dependency order with every table resident. This module does
the same ordering without keeping every wide frame. Pass 1 (`run_cycle_pass1`) loads only the
cyclic key columns of a component and runs the ordinary work nodes for them, then leaves each
parent's effective source->masked map in the run's `parent_map_cache`. Pass 2 is the unchanged
table loop in `_sequential`: it masks each table from its originals and resolves FK children
through `resolve_fk_keys` against those cached maps. A matched child therefore inherits the
parent's emitted value (a token, a raw value left by a `when:` gate or a preserved orphan, or
nothing for an errored parent) instead of re-deriving it, and nothing here iterates to a fixed
point.

Pass 1 reuses `_dispatch_mask_node` and `_parent_map` on narrow frames, so a map is built by the
same code that builds it on the full-frame route. Admission (`_sequential_cycle_admit`) restricts
key producers to pure functions of the key value, which makes the narrow run's maps equal the
full-frame ones; the producer columns themselves are still masked from originals in pass 2.

Established methodology: components are found with Tarjan's strongly connected components
algorithm (the one `networkx.strongly_connected_components` implements), and tables are ordered by
a Kahn topological sort of the condensation, the same sort `order_work` uses for work nodes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from decoy_engine.errors import RowErrorsFailedError
from decoy_engine.execution._adapter import StrategyContext
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._fk_keys import fk_columns_for_table, to_pandas_fk_safe
from decoy_engine.execution._guards import reject_null_bearing_int
from decoy_engine.execution._row_errors import drain_row_errors
from decoy_engine.execution._sequential_composite_chain import decline_composite_chain

if TYPE_CHECKING:
    import pandas as pd

    from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
    from decoy_engine.execution._runner import WorkNode
    from decoy_engine.execution._sequential_cycle_admit import CycleContext
    from decoy_engine.execution._sequential_native import SeqNativeRun
    from decoy_engine.generation.pool._cache import PoolCache
    from decoy_engine.plan._types import Plan
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import NamespaceRegistry, RelationshipGraph

_NodeKey = tuple[str, tuple[str, ...]]
_KeyTuple = tuple[object, ...]

__all__ = [
    "cyclic_key_columns",
    "fk_components",
    "map_still_needed",
    "release_parent_maps",
    "run_cycle_pass1",
    "sequential_table_order",
    "table_topo_order",
]


def fk_components(graph: RelationshipGraph) -> list[tuple[str, ...]]:
    """The strongly connected components of the TABLE graph that are real cycles (two or more
    tables). A self-referencing table is not one: it masks within a single table."""
    succ: dict[str, list[str]] = {}
    for edge in graph.edges:
        succ.setdefault(edge.parent_table, [])
        succ.setdefault(edge.child_table, [])
        if (
            edge.parent_table != edge.child_table
            and edge.child_table not in succ[edge.parent_table]
        ):
            succ[edge.parent_table].append(edge.child_table)
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    found: list[tuple[str, ...]] = []
    for root in succ:
        if root in index:
            continue
        index[root] = low[root] = len(index)
        stack.append(root)
        on_stack.add(root)
        work = [(root, iter(succ[root]))]
        while work:
            node, children = work[-1]
            descended = False
            for nxt in children:
                if nxt not in index:
                    index[nxt] = low[nxt] = len(index)
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(succ[nxt])))
                    descended = True
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
            if descended:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                members: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    members.append(member)
                    if member == node:
                        break
                if len(members) > 1:
                    found.append(tuple(sorted(members)))
    return found


def cyclic_key_columns(
    graph: RelationshipGraph, component: tuple[str, ...]
) -> dict[str, tuple[str, ...]]:
    """Per table, the columns that are a parent or child key of an edge inside `component`."""
    members = set(component)
    keys: dict[str, set[str]] = {}
    for edge in graph.edges:
        if edge.parent_table in members and edge.child_table in members:
            keys.setdefault(edge.parent_table, set()).update(edge.parent_columns)
            keys.setdefault(edge.child_table, set()).update(edge.child_columns)
    return {t: tuple(sorted(cols)) for t, cols in keys.items()}


def _seed_order(plan: Plan, graph: RelationshipGraph) -> list[str]:
    """Plan table order first, then any table only an edge names: the tie-break for both sorts."""
    order: list[str] = []
    seen: set[str] = set()
    for name, _ in plan.seed_envelope.per_table:
        if name not in seen:
            seen.add(name)
            order.append(name)
    for edge in graph.edges:
        for tbl in (edge.parent_table, edge.child_table):
            if tbl not in seen:
                seen.add(tbl)
                order.append(tbl)
    return order


def table_topo_order(plan: Plan, graph: RelationshipGraph) -> list[str]:
    """FK-topological order over TABLES (every parent before its children), stable
    to the plan's table order where the graph leaves a choice. Raises on a cycle.
    Sequential eviction relies on a parent being fully masked, and its key map
    built, before any child table is loaded."""
    order_seed = _seed_order(plan, graph)
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
            code="relationship_cycle",
            message=(
                "FK relationship graph has a cycle across tables; cannot order "
                "tables for sequential masking."
            ),
        )
    return result


def sequential_table_order(
    plan: Plan, graph: RelationshipGraph
) -> tuple[list[str], list[tuple[str, ...]]]:
    """Table order for the SEQ loop, and the cyclic components in the order they are reached.

    An acyclic graph keeps `table_topo_order` exactly. With cycles, each component is one unit
    of a Kahn sort over the condensation, its tables emitted together in plan order."""
    components = fk_components(graph)
    if not components:
        return table_topo_order(plan, graph), []
    seed = _seed_order(plan, graph)
    position = {t: i for i, t in enumerate(seed)}
    unit_of: dict[str, tuple[str, ...]] = {t: (t,) for t in seed}
    for comp in components:
        ordered = tuple(sorted(comp, key=position.__getitem__))
        unit_of.update(dict.fromkeys(comp, ordered))
    units = {unit_of[t] for t in seed}
    children: dict[tuple[str, ...], set[tuple[str, ...]]] = {u: set() for u in units}
    indegree = dict.fromkeys(units, 0)
    for edge in graph.edges:
        parent, child = unit_of[edge.parent_table], unit_of[edge.child_table]
        if parent != child and child not in children[parent]:
            children[parent].add(child)
            indegree[child] += 1

    def rank(unit: tuple[str, ...]) -> int:
        return position[unit[0]]

    ready = sorted((u for u in units if indegree[u] == 0), key=rank)
    order: list[str] = []
    reached: list[tuple[str, ...]] = []
    while ready:
        unit = ready.pop(0)
        order.extend(unit)
        if len(unit) > 1:
            reached.append(unit)
        for child in children[unit]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
        ready.sort(key=rank)
    return order, reached


def map_still_needed(
    edge_parent: str,
    edge_columns: tuple[str, ...],
    remaining: dict[_NodeKey, set[str]],
) -> bool:
    """Whether some child of this parent key is still to be processed.

    On an acyclic graph every child comes after its parent, so this is always true when a table
    finishes. On a cycle a back-edge's child may already be done; its map was released then and
    must not be rebuilt."""
    return bool(remaining.get((edge_parent, edge_columns)))


def release_parent_maps(
    graph: RelationshipGraph,
    table: str,
    remaining: dict[_NodeKey, set[str]],
    parent_map_cache: dict[_NodeKey, dict[_KeyTuple, _KeyTuple]],
    errored_keys_cache: dict[_NodeKey, dict[_KeyTuple, str]],
) -> None:
    """Drop every parent map whose last child consumer is `table`.

    A map is read by pass 1, by pass 2 of each child table, and by outgoing acyclic children, all
    of which are children of its edge, so the child-set bookkeeping covers all three."""
    for edge in graph.edges:
        if edge.child_table == table:
            key = (edge.parent_table, edge.parent_columns)
            consumers = remaining.get(key)
            if consumers is not None:
                consumers.discard(table)
                if not consumers:
                    parent_map_cache.pop(key, None)
                    errored_keys_cache.pop(key, None)


def run_cycle_pass1(
    adapter: PandasExecutionAdapter,
    plan: Plan,
    graph: RelationshipGraph,
    component: tuple[str, ...],
    *,
    ctx: CycleContext,
    ordered: list[WorkNode],
    node_by_key: dict[_NodeKey, WorkNode],
    registry: ProviderRegistry,
    namespace_registry: NamespaceRegistry,
    pool_cache: PoolCache,
    mask_key: bytes,
    native_run: SeqNativeRun,
    parent_map_cache: dict[_NodeKey, dict[_KeyTuple, _KeyTuple]],
    errored_keys_cache: dict[_NodeKey, dict[_KeyTuple, str]],
) -> None:
    """Build the effective key maps of one component from a key-columns-only read.

    The work nodes of the key columns run in `order_work` order across all the component's
    narrow frames, so each map is built after the column that feeds it, as on the full-frame
    route. Row errors are fail-loud here: admission rules out quarantine, so any record is
    uncovered, exactly as the table loop classifies it. The narrow frames are dropped on return;
    only the maps stay."""
    read_columns = ctx.projected_loader
    if read_columns is None:  # admission declines this first; kept so a bypass fails closed
        from decoy_engine.execution._sequential_cycle_admit import PROJECTED_READ

        raise ExecutionError(
            code=PROJECTED_READ, message="the cyclic key pass has no column-projected read."
        )
    keys = cyclic_key_columns(graph, component)
    _require_incoming_maps(graph, keys, parent_map_cache)
    pass_ctx = StrategyContext(
        registry=registry,
        pool_cache=pool_cache,
        relationship_graph=graph,
        namespace_registry=namespace_registry,
        job_seed=plan.seed_envelope.job_seed,
        mask_key=mask_key,
        seq_native=native_run,
    )
    frames: dict[str, pd.DataFrame] = {}
    snapshots: dict[tuple[str, str], pd.Series] = {}
    for table in component:
        narrow = read_columns(table, keys[table])
        reject_null_bearing_int(plan, {table: narrow}, registry, graph)
        df = to_pandas_fk_safe(narrow, fk_columns_for_table(graph.edges, table))
        frames[table] = df
        # The key pass shares the run's hash decisions. A REMAP orphan minted here must take the
        # backend pass 2 will take for the same key (an incoming parent's decision already exists),
        # and a back-edge child REMAPs against a parent masked after it, so the component's key
        # columns are decided now (see `prepare_table`).
        native_run.prepare_table(plan, table, narrow.schema, graph, record_inherited=False)
        for edge in graph.edges:
            if edge.parent_table == table:
                for col in edge.parent_columns:
                    if col in df.columns and (table, col) not in snapshots:
                        snapshots[(table, col)] = df[col].copy()
    key_error_rows: dict[str, dict[str, dict[int, str]]] = {}
    for node in ordered:
        wanted = keys.get(node.table)
        if wanted is None or not set(node.columns) <= set(wanted):
            continue
        with decline_composite_chain(ordered, graph, node.table):
            adapter._dispatch_mask_node(
                node,
                frames,
                graph,
                snapshots,
                parent_map_cache,
                node_by_key,
                pass_ctx,
                key_error_rows=key_error_rows,
                errored_keys_cache=errored_keys_cache,
            )
        batch = drain_row_errors(pass_ctx.row_errors, table=node.table)
        if batch:
            raise RowErrorsFailedError(tuple(batch))
    members = set(component)
    for edge in graph.edges:
        if edge.parent_table in members and edge.child_table in members:
            adapter._parent_map(
                edge,
                frames,
                snapshots,
                parent_map_cache,
                key_error_rows=key_error_rows,
                errored_keys_cache=errored_keys_cache,
            )


def _require_incoming_maps(
    graph: RelationshipGraph,
    keys: dict[str, tuple[str, ...]],
    parent_map_cache: dict[_NodeKey, dict[_KeyTuple, _KeyTuple]],
) -> None:
    """An acyclic parent outside the component must already have left its map: the narrow frames
    do not hold that parent, and a missing map would read as 'every key is an orphan'."""
    for edge in graph.edges:
        if edge.child_table in keys and edge.parent_table not in keys:
            if (edge.parent_table, edge.parent_columns) not in parent_map_cache:
                raise ExecutionError(
                    code="sequential_cycle_incoming_map_missing",
                    message=(
                        f"the key map of {edge.parent_table}.{list(edge.parent_columns)} was not "
                        f"built before the cyclic table {edge.child_table!r}; the table order is "
                        "inconsistent with the relationship graph."
                    ),
                )

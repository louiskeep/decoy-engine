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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, TypeVar

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.plan._types import ColumnSeed, GroupSeed

if TYPE_CHECKING:
    from decoy_engine.execution.native._requirements import NodeRequirements
    from decoy_engine.plan._types import Plan
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import RelationshipGraph

_NodeKey = tuple[str, tuple[str, ...]]


class _HasColumns(Protocol):
    @property
    def columns(self) -> tuple[str, ...]: ...


_N = TypeVar("_N", bound=_HasColumns)


def work_order_key(table: str, columns: tuple[str, ...]) -> _NodeKey:
    """The identity AND sort key of a work node. `order_work`'s tie-break
    (`_kahn_sorted`) takes the smallest of these, so any route that must visit
    nodes in the oracle's order sorts by this one key instead of restating it."""
    return (table, columns)


def in_work_order(table: str, nodes: Iterable[_N]) -> list[_N]:
    """`nodes` of one table in `order_work` order, for a route with no FK or
    composite dependency edges (the unified slice), where that order is exactly
    key order. Visit order is observable: a provider with state, or two columns
    that both fail, behave differently under plan (config) order."""
    return sorted(nodes, key=lambda n: work_order_key(table, n.columns))


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
    # Native planning boundary (Task 0.2b): the resolved native execution
    # requirements for this node, attached inertly at native-plan compile time.
    # None on every existing execution path (build_work_list never sets it), so
    # this is pure addition: no route reads it, and byte output is unchanged.
    requirements: NodeRequirements | None = None

    @property
    def key(self) -> _NodeKey:
        return work_order_key(self.table, self.columns)


def provider_is_composite(provider: str | None, registry: ProviderRegistry) -> bool:
    """Whether a column's provider fans out to a multi-column composite node.

    The single source of the provider-composite predicate: a registry-bound
    provider whose capability ``backend_type == "composite"``. ``build_work_list``
    uses it to set ``WorkNode.kind == "composite"``, and the native planning
    boundary reads the same helper so ``native_route_eligibility`` and
    ``compile_native_plan`` never disagree on whether a column is composite.
    Null-guarded before ``registry.has``, which expects a provider string.
    """
    if not provider or not registry.has(provider):
        return False
    return registry.get_capabilities(provider).backend_type == "composite"


def build_work_list(plan: Plan, registry: ProviderRegistry) -> list[WorkNode]:
    """Enumerate every maskable unit from the seed envelope (S9 spec §6.1).

    Covers single-table no-FK jobs (which `plan.ordering` does not). Composite
    output columns collapse into one bundle node keyed on the sorted union of
    (column + its coherent_with); a composite is recognized by
    `provider_is_composite` (registry `backend_type == "composite"`).
    """
    work: list[WorkNode] = []
    for table, table_seed in plan.seed_envelope.per_table:
        composite_groups: dict[_NodeKey, list[ColumnSeed]] = {}
        for col_name, col_seed in table_seed.per_column:
            if provider_is_composite(col_seed.provider, registry):
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


def date_shift_group_columns(plan: Plan, registry: ProviderRegistry) -> dict[str, set[str]]:
    """Per-table set of sibling columns named by a `date_shift` `group_by` (HC-3a).

    A `group_by` anchor column has the same two requirements as an FK-parent
    key column: it must be read PRE-MASK (so every row of one entity anchors on
    the identical source value regardless of masking order / when-gates) and
    converted LOSSLESSLY (an int+null anchor must not widen to float64, which
    would make `_canonicalize_source` reject a valid id). Both the pandas
    adapter and the sequential runner union this set into
    `fk_columns_for_table(...)` (lossless ingestion) and snapshot these columns
    pre-mask into `StrategyContext.group_anchor_snapshots`. Empty when no
    `date_shift` column configures a `group_by`. Only a non-empty STRING
    `group_by` is collected; a malformed ref is rejected at plan-compile
    (`check_date_shift_group_by_refs`), so this walker never has to guess.
    """
    from decoy_engine.execution._adapter import provider_config_to_dict

    out: dict[str, set[str]] = {}
    for node in build_work_list(plan, registry):
        if node.kind != "scalar" or node.strategy != "date_shift":
            continue
        if not isinstance(node.plan_slice, ColumnSeed):
            continue
        group_by = provider_config_to_dict(node.plan_slice.provider_config).get("group_by")
        if isinstance(group_by, str) and group_by:
            out.setdefault(node.table, set()).add(group_by)
    return out


def top_code_columns(plan: Plan, registry: ProviderRegistry) -> dict[str, set[str]]:
    """Per-table set of columns masked by `top_code` (HC-3b).

    A top_code column must ingest LOSSLESSLY (nullable Int64), never via a bare
    `to_pandas()` that widens an int+null column to float64. Widening rounds any
    integer with magnitude >= 2**53, and -- because whether a chunk widens
    depends on whether THAT chunk carries a null -- makes the masked output
    depend on the chunk boundary, breaking the CHUNK_SAFE byte-identity contract
    (Codex R2 HIGH). The pandas adapter and the sequential runner union this set
    into `fk_columns_for_table(...)` so the handler compares and renders exact
    integer values on every route. Genuine float columns are unaffected (they are
    float64 on every route already, so no chunk-dependent widening arises).
    """
    out: dict[str, set[str]] = {}
    for node in build_work_list(plan, registry):
        if node.kind != "scalar" or node.strategy != "top_code":
            continue
        for col in node.columns:
            out.setdefault(node.table, set()).add(col)
    return out


def group_key_group_by_columns(plan: Plan, registry: ProviderRegistry) -> dict[str, set[str]]:
    """Per-table set of sibling columns named by a `group_key` `group_by` (Trap B).

    A group_by column needs the same lossless-ingest treatment as an FK
    parent key or a `date_shift`/`top_code` column: `apply_group_key` calls
    `str(raw_val)` on the group_by cell, so an int+null column that widens
    to float64 on a null-bearing chunk but stays int64 on a null-free one
    stringifies "5" vs "5.0" for the SAME value depending purely on which
    OTHER rows share its chunk -- a chunk-boundary-dependent key, breaking
    the sibling-keyed byte-identity contract. Both the pandas adapter and
    the sequential runner union this set into `fk_columns_for_table(...)`
    (lossless ingestion only -- NOT a pre-mask snapshot: unlike date_shift's
    group anchors, group_key reads the group_by column at its own position
    in the work order on every route, so a masked group_by column is read
    post-mask consistently everywhere).
    """
    from decoy_engine.execution._adapter import provider_config_to_dict

    out: dict[str, set[str]] = {}
    for node in build_work_list(plan, registry):
        if node.kind != "scalar" or node.strategy != "group_key":
            continue
        if not isinstance(node.plan_slice, ColumnSeed):
            continue
        group_by = provider_config_to_dict(node.plan_slice.provider_config).get("group_by")
        if isinstance(group_by, str) and group_by:
            out.setdefault(node.table, set()).add(group_by)
    return out


def joint_mask_key_by_columns(plan: Plan, registry: ProviderRegistry) -> dict[str, set[str]]:
    """Per-table set of `joint_mask` `key_by` SOURCE-key columns (cross-column V1).

    `key_by` selects the reference tuple off the SOURCE key value (its docstring:
    "key_by: Source column"; the FK-key identity analogy -- same source entity ->
    same reference row), so it must read a PRE-MASK snapshot, never the mutable
    frame. This set is unioned into the group-anchor snapshot build + the
    lossless-ingest column set on the full-frame and sequential routes, EXACTLY
    as `date_shift` `group_by` is (`source_anchor_columns`): the reused precedent,
    not a new snapshot stage. Empty when no `joint_mask` column configures
    `key_by`.
    """
    from decoy_engine.execution._adapter import provider_config_to_dict

    out: dict[str, set[str]] = {}
    for node in build_work_list(plan, registry):
        if node.strategy != "joint_mask" or not isinstance(node.plan_slice, ColumnSeed):
            continue
        key_by = provider_config_to_dict(node.plan_slice.provider_config).get("key_by")
        if isinstance(key_by, str) and key_by:
            out.setdefault(node.table, set()).add(key_by)
    return out


def source_anchor_columns(plan: Plan, registry: ProviderRegistry) -> dict[str, set[str]]:
    """All columns a strategy reads at their immutable SOURCE value through the
    pre-mask `ctx.group_anchor_snapshots`: `date_shift` `group_by` anchors plus
    `joint_mask` `key_by` source keys. Both read a sibling's source value via the
    same pre-mask snapshot machinery, so they share one build set (the
    `date_shift` precedent reused for `joint_mask`; see the plan's §5 no-fork
    checklist). `group_key`'s `group_by` is NOT here: it reads the MASKED value
    under an ordering edge, not a source snapshot.
    """
    out = date_shift_group_columns(plan, registry)
    for table, cols in joint_mask_key_by_columns(plan, registry).items():
        out.setdefault(table, set()).update(cols)
    return out


def _writer_node_map(work: list[WorkNode]) -> dict[tuple[str, str], _NodeKey]:
    """Map each written (table, column) to the key of the node that WRITES it.

    Not column=node (the cross-column ordering trap): a composite provider is a
    bundle node whose `columns` holds several outputs, so every one of them maps
    to that ONE bundle key; a scalar node (`derived`/`group_key`/`joint_mask`)
    maps its single output column to itself. A dependent's driver ref resolves
    through this map to the node that must run first.
    """
    writer: dict[tuple[str, str], _NodeKey] = {}
    for node in work:
        for col in node.columns:
            writer[(node.table, col)] = node.key
    return writer


def _masked_driver_columns(node: WorkNode) -> frozenset[str]:
    """The same-table sibling columns `node` reads for their MASKED value, so it
    must be ordered AFTER them (the cross-column V1 ordering edge).

    `derived` (any expression, including the `case_when` construct): the
    expression's column refs, the same set `check_derived_column_refs` extracts.
    `group_key`: its `group_by` sibling (its docstring pins a post-mask read).
    Every other node (including `joint_mask`, whose `key_by` is a SOURCE read via
    snapshot, so it imposes no ordering edge) reads no masked driver here.
    """
    plan_slice = node.plan_slice
    if not isinstance(plan_slice, ColumnSeed):
        return frozenset()
    from decoy_engine.execution._adapter import provider_config_to_dict

    if node.strategy == "derived":
        from decoy_engine.expressions import compile_expr
        from decoy_engine.transforms.derived import _get_column_refs

        expr = provider_config_to_dict(plan_slice.provider_config).get("expression")
        if not expr:
            return frozenset()
        try:
            return _get_column_refs(compile_expr(str(expr)))
        except Exception:
            # Invalid syntax surfaces as a ValidationError at execution time
            # (DerivedConfig.from_dict); no edge, no double-report.
            return frozenset()
    if node.strategy == "group_key":
        group_by = provider_config_to_dict(plan_slice.provider_config).get("group_by")
        if isinstance(group_by, str) and group_by:
            return frozenset({group_by})
    return frozenset()


def column_dependency_edges(work: list[WorkNode]) -> dict[_NodeKey, set[_NodeKey]]:
    """The cross-column ordering channel for `order_work`: dependent-node key ->
    the keys of the nodes that write its MASKED driver columns.

    A driver column ref resolves to its writer node through `_writer_node_map`
    (per-provider representation, not column=node). A ref whose column no node
    writes (an unmasked passthrough source) imposes no edge -- its live value is
    already its source value -- and a self-ref is skipped. SOURCE-read drivers
    (`joint_mask` `key_by`) are deliberately absent: they read a pre-mask
    snapshot, so they need no ordering edge. Reusing `order_work`'s own topo sort
    + cycle backstop (no parallel scheduler); derived-derived cycles are still
    caught at plan time by `check_derived_column_refs`.
    """
    writer = _writer_node_map(work)
    deps: dict[_NodeKey, set[_NodeKey]] = defaultdict(set)
    for node in work:
        for col in _masked_driver_columns(node):
            writer_key = writer.get((node.table, col))
            if writer_key is not None and writer_key != node.key:
                deps[node.key].add(writer_key)
    return deps


def order_work(
    work: list[WorkNode],
    relationship_graph: RelationshipGraph,
    column_deps: Mapping[_NodeKey, set[_NodeKey]] | None = None,
) -> list[WorkNode]:
    """Order the work list (S9 spec §6.2).

    Dependency edges come from `relationship_graph.edges` (FK parent -> child),
    plus a composite-node -> FK-child edge whenever the composite's output
    columns are a superset of an FK's parent_columns (R17: the composite writes
    the whole bundle before any child reads a masked parent column). Topo-sorted
    with a sorted tie-break for byte-stable ordering.

    `column_deps` is the cross-column V1 channel: dependent-node key -> the keys
    of the nodes writing its MASKED driver columns (`derived` expression refs,
    `group_key` `group_by`), so a dependent reads its drivers' masked values
    under an explicit edge instead of by lexicographic-sort accident. None means
    derive them from `work` via `column_dependency_edges` (so every route gets
    the fix); a caller/test may pass an explicit map. These edges only ADD
    constraints, so FK ordering stays dominant; a cross-column edge that
    contradicts an FK edge is a genuine cycle and raises `cyclic_work_ordering`.
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

    # Cross-column V1: a dependent waits on the node(s) writing its MASKED
    # drivers, so the read is intentional (not lexicographic luck). FK edges
    # above stay dominant: these only add constraints.
    col_deps = column_dependency_edges(work) if column_deps is None else column_deps
    for dependent, drivers in col_deps.items():
        if dependent not in by_key:
            continue
        for driver in drivers:
            if driver in by_key:
                deps[dependent].add(driver)

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

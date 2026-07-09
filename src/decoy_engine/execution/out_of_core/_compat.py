"""Compatibility gate for the out-of-core FK route.

The gate lives beside the polars adapter instead of inside the future execution
operator so routing has one decision surface: polars-native, out-of-core, or
oracle/rejection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from decoy_engine.execution._runner import WorkNode
    from decoy_engine.plan._types import ColumnSeed, Plan
    from decoy_engine.relationships import RelationshipGraph
    from decoy_engine.relationships._graph import RelationshipEdge


_CROSS_ROW_STRATEGIES = frozenset(
    {
        "grouped_series",
        "windowed_date",
        "derived_aggregate",
        "group_key",
        "joint_mask",
    }
)
_INITIAL_SUPPORTED_STRATEGIES = frozenset({"hash", "redact", "truncate", "passthrough"})


@dataclass(frozen=True)
class OutOfCoreRejection:
    code: str
    message: str


@dataclass(frozen=True)
class OutOfCoreCompatibility:
    accepted: bool
    rejections: tuple[OutOfCoreRejection, ...] = ()

    @property
    def primary_code(self) -> str | None:
        return self.rejections[0].code if self.rejections else None

    def message(self) -> str:
        return "; ".join(r.message for r in self.rejections)


def check_out_of_core_compatibility(
    plan: Plan,
    work: list[WorkNode],
    relationship_graph: RelationshipGraph,
) -> OutOfCoreCompatibility:
    """Return the current Option 4 compatibility decision.

    Initial acceptance covers kernelized per-value parent key strategies and no
    multiple-parent resolution for the same child FK tuple yet.
    """
    rejections: list[OutOfCoreRejection] = []
    if not relationship_graph.edges:
        return OutOfCoreCompatibility(
            accepted=False,
            rejections=(
                OutOfCoreRejection(
                    "out_of_core_no_relationships",
                    "out-of-core FK route requires at least one relationship edge.",
                ),
            ),
        )

    child_targets: dict[tuple[str, tuple[str, ...]], int] = {}
    composite_fk_child_columns: dict[str, set[str]] = {}
    for edge in relationship_graph.edges:
        key = (edge.child_table, edge.child_columns)
        child_targets[key] = child_targets.get(key, 0) + 1
        if len(edge.child_columns) > 1:
            composite_fk_child_columns.setdefault(edge.child_table, set()).update(
                edge.child_columns
            )
    if any(count > 1 for count in child_targets.values()):
        rejections.append(
            OutOfCoreRejection(
                "out_of_core_multi_parent_child_unsupported",
                "out-of-core route does not yet support multiple parents for one child FK.",
            )
        )

    for edge in relationship_graph.edges:
        _check_edge(edge, plan, rejections)

    if _table_graph_has_cycle(relationship_graph):
        # `_table_order` (out_of_core/_runner.py) sequences whole TABLES, so any
        # table-level FK cycle across two or more tables (A.id->B.x, B.id->A.y)
        # makes `_table_order` raise `out_of_core_relationship_cycle` at run
        # time -- even when the COLUMN-level dependency the full-frame oracle
        # orders on is acyclic and the oracle succeeds. Reject fail-closed here
        # (the multi-table generalization of the self-referential edge rule
        # above) so the route never runs on a config it would crash on; the job
        # falls back to full-frame, which handles it natively.
        rejections.append(
            OutOfCoreRejection(
                "out_of_core_relationship_cycle_unsupported",
                "out-of-core route requires an acyclic table-level FK graph; a "
                "multi-table FK cycle cannot be expressed by table-level dependency "
                "ordering (even when column-level ordering is acyclic). Falls back "
                "to full-frame.",
            )
        )

    for node in work:
        # GroupSeed (composite_fk_group) has no `when` field; getattr handles
        # both plan_slice shapes without a kind-based isinstance branch.
        if getattr(node.plan_slice, "when", None) is not None:
            rejections.append(
                OutOfCoreRejection(
                    "out_of_core_when_predicate_unsupported",
                    f"{node.table}.{node.columns} carries a `when` predicate; the "
                    "out-of-core route masks every non-null row unconditionally and "
                    "has no row-gating, so admitting it would silently over-mask.",
                )
            )
            continue
        if node.kind == "composite_fk_group":
            if not _composite_group_join_covered(node, relationship_graph):
                rejections.append(
                    OutOfCoreRejection(
                        "out_of_core_composite_group_uncovered",
                        f"composite_fk_group {node.table}.{node.columns} has a column "
                        "not covered as a child FK column by any relationship edge; "
                        "join coverage can't be proven safe out-of-core.",
                    )
                )
            continue
        if node.kind != "scalar":
            rejections.append(
                OutOfCoreRejection(
                    "out_of_core_non_scalar_work_unsupported",
                    f"out-of-core route does not yet support {node.kind} work nodes.",
                )
            )
            continue
        if any(col in composite_fk_child_columns.get(node.table, ()) for col in node.columns):
            # A COMPOSITE FK child column masked as an independent scalar strategy
            # (rather than as one composite_fk_group over the whole key) is the
            # one composite shape that diverges from the pandas oracle. The oracle
            # masks each such column with its own strategy BEFORE resolving the FK
            # (FK children resolve last), so a PRESERVE/WARN orphan keeps the
            # scalar-MASKED value; the out-of-core route joins on and preserves the
            # RAW source value for the same orphan -- a raw-value leak (and, for a
            # partial-null key, a null-vs-masked divergence). The canonical
            # composite_fk_group shape (a single GroupSeed over the FK columns) is
            # oracle-parity across orphans, partial-nulls, and every policy, and
            # stays admitted; single-column scalar FK children stay admitted too
            # (they are FK-resolution-owned, not double-masked). Reject fail-closed
            # so the job falls back to full-frame instead of leaking. See CF2 in
            # docs/plans/2026-07-07-next-up-roadmap.md and
            # tests/parity/SEMANTIC_DIFFERENCES.md.
            rejections.append(
                OutOfCoreRejection(
                    "out_of_core_composite_fk_scalar_child_unsupported",
                    f"{node.table}.{node.columns} is a child column of a composite "
                    "FK edge but is masked as an independent scalar strategy; the "
                    "out-of-core route preserves the RAW source value for orphans "
                    "here while the pandas oracle preserves the scalar-masked value "
                    "(a raw-value leak divergence). Use a composite_fk_group over "
                    "the FK columns, which is oracle-parity; falls back to full-frame.",
                )
            )
            continue
        if node.strategy in _CROSS_ROW_STRATEGIES:
            rejections.append(
                OutOfCoreRejection(
                    "out_of_core_cross_row_strategy_unsupported",
                    f"strategy {node.strategy!r} needs a bounded relational lowering.",
                )
            )
        elif node.strategy not in _INITIAL_SUPPORTED_STRATEGIES:
            rejections.append(
                OutOfCoreRejection(
                    "out_of_core_strategy_unsupported",
                    f"strategy {node.strategy!r} is not in the initial out-of-core slice.",
                )
            )

    return OutOfCoreCompatibility(accepted=not rejections, rejections=tuple(rejections))


def _table_graph_has_cycle(relationship_graph: RelationshipGraph) -> bool:
    """True if the child->parent TABLE dependency graph has a multi-table cycle.

    Mirrors `_table_order`'s Kahn topological sort (out_of_core/_runner.py) at
    gate time, so a cycle the runner would crash on is rejected before it runs.
    Self-edges (parent_table == child_table) are excluded: those are reported
    separately by `_check_edge`'s self-referential rejection, and a self-loop is
    not the multi-table shape this check owns.
    """
    tables: set[str] = set()
    deps: dict[str, set[str]] = {}
    children: dict[str, set[str]] = {}
    for edge in relationship_graph.edges:
        tables.add(edge.parent_table)
        tables.add(edge.child_table)
        deps.setdefault(edge.parent_table, set())
        deps.setdefault(edge.child_table, set())
        children.setdefault(edge.parent_table, set())
        children.setdefault(edge.child_table, set())
        if edge.parent_table == edge.child_table:
            continue
        deps[edge.child_table].add(edge.parent_table)
        children[edge.parent_table].add(edge.child_table)
    ready = [table for table in tables if not deps[table]]
    ordered = 0
    while ready:
        table = ready.pop()
        ordered += 1
        for child in children[table]:
            deps[child].discard(table)
            if not deps[child]:
                ready.append(child)
    return ordered != len(tables)


def _composite_group_join_covered(node: WorkNode, relationship_graph: RelationshipGraph) -> bool:
    """True if every column of a composite_fk_group node is a child FK column
    of some edge on that table (the join the out-of-core route relies on to
    resolve the group actually exists, rather than being assumed)."""
    covered: set[str] = set()
    for edge in relationship_graph.edges:
        if edge.child_table == node.table:
            covered.update(edge.child_columns)
    return all(column in covered for column in node.columns)


def _check_edge(
    edge: RelationshipEdge,
    plan: Plan,
    rejections: list[OutOfCoreRejection],
) -> None:
    if len(edge.parent_columns) != len(edge.child_columns):
        rejections.append(
            OutOfCoreRejection(
                "out_of_core_fk_arity_mismatch",
                "parent and child FK column counts must match.",
            )
        )
    if edge.parent_table == edge.child_table:
        # `_table_order` (out_of_core/_runner.py) sequences whole TABLES, so a
        # self-referential edge makes a table depend on itself and always
        # raises `out_of_core_relationship_cycle` -- even though the column-
        # level dependency (parent column masked before the FK column resolves
        # against it) has no cycle and the full-frame oracle handles it fine.
        # Reject fail-closed here instead of letting the route crash on an
        # admitted config; the job falls back to full-frame, which supports
        # this shape natively.
        rejections.append(
            OutOfCoreRejection(
                "out_of_core_self_referential_fk_unsupported",
                f"{edge.parent_table}: self-referential FK edges are not supported "
                "out-of-core (table-level dependency ordering cannot express a "
                "table depending on itself); falls back to full-frame.",
            )
        )
    for parent_column in edge.parent_columns:
        parent_seed = _column_seed(plan, edge.parent_table, parent_column)
        if parent_seed is None:
            rejections.append(
                OutOfCoreRejection(
                    "out_of_core_parent_seed_missing",
                    f"parent key {edge.parent_table}.{parent_column} is not in the plan.",
                )
            )
        elif parent_seed.strategy not in _INITIAL_SUPPORTED_STRATEGIES:
            rejections.append(
                OutOfCoreRejection(
                    "out_of_core_parent_strategy_unsupported",
                    f"out-of-core route does not support parent FK key strategy "
                    f"{parent_seed.strategy!r}.",
                )
            )
        elif parent_seed.strategy == "hash" and parent_seed.namespace is None:
            rejections.append(
                OutOfCoreRejection(
                    "out_of_core_parent_namespace_missing",
                    "hash parent FK key must carry a namespace.",
                )
            )


def _column_seed(plan: Plan, table: str, column: str) -> ColumnSeed | None:
    for table_name, table_seed in plan.seed_envelope.per_table:
        if table_name != table:
            continue
        for col_name, col_seed in table_seed.per_column:
            if col_name == column:
                return col_seed
    return None

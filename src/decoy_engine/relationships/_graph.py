"""Relationship graph: topologically-ordered DAG of FK relationships.

The graph is the planning-layer data structure that answers two questions
S3-S13 ask repeatedly:

- "What parent tables must mask before this child table can mask?"
  (`children_of` for the upstream direction; `parents_of` for the lookup)
- "What is the deterministic order to walk all relationships?"
  (`ordering` for the topological sort)

Composite parents collapse to one node: child columns wait on the whole
parent tuple, not on individual columns of a composite PK.

Source patterns:

- DAG topological sort follows Kahn's algorithm with sorted-queue
  determinism for byte-stable ordering across Python runtimes (same shape
  S1's `check_fk_plan_ordering` used; this module replaces that inline
  implementation per S2 §4 wiring).
- The immutable-edge + graph-as-frozen-artifact shape draws from dbt's
  `manifest.json` dependency graph (every node + every edge committed
  to the artifact; downstream consumers read, never mutate).
- Multi-parent FK rejection draws from RDBMS schema validation: a child
  column referencing multiple distinct parent tables is rejected as a
  declaration error rather than resolved by silent first-parent-wins.

`is_descendant` is intentionally NOT in this module's public surface
(per S2 spec M3 resolution): no concrete consumer in S2-S10 cites it;
narrow interfaces (best-practices §3.3) keep speculative public API out
of the post-GA deprecation surface. `parents_of` + `children_of` cover
the cited S9 execution-ordering use case.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from typing import Any

from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.profile._types import Relationship
from decoy_engine.relationships._namespace import NamespaceRegistry


class OrphanPolicy(Enum):
    """Runtime enum of orphan-FK handling policies.

    String values match `decoy_engine.plan._types.OrphanPolicy` literal
    (the YAML-serializable form) and `tests/fixtures/golden/_manifest_schema`
    `OrphanPolicyLiteral` (the manifest-validation form). The three
    representations are kept in sync by tests that exercise each value
    end-to-end.

    Per S2 spec §3 (resolution of M2): the `remap` policy uses the
    sentinel format `masked_orphan_<id>`; bumping that format is a
    release-notes line per `done-definition.md`.
    """

    PRESERVE = "preserve"
    REMAP = "remap"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class RelationshipEdge:
    """One resolved FK relationship in the planner-layer graph.

    Mirrors `decoy_engine.profile.Relationship` (the data-shape side) with
    two planner-layer fields added: `namespace` is always non-None at this
    layer (the registry resolved it) and `orphan_policy` is required (the
    `orphan_fk_policy_completeness` check guarantees the config supplied
    one before the graph was built).

    `parent_columns` and `child_columns` are tuples of length 1 for
    single-column FKs and >1 for composite-key FKs. Both tuples must
    have the same length (enforced by the source `Relationship`'s
    `__post_init__`).
    """

    parent_table: str
    parent_columns: tuple[str, ...]
    child_table: str
    child_columns: tuple[str, ...]
    namespace: str
    orphan_policy: OrphanPolicy


@dataclass(frozen=True)
class RelationshipGraph:
    """Frozen DAG of FK relationships + topological ordering.

    `edges` carries every resolved relationship as a `RelationshipEdge`.
    `ordering` is the (table, columns) walk order: parents before children,
    composite parents collapsed to one node. Both are deterministic across
    runs given equal input.

    Query helpers `parents_of` and `children_of` are O(edges) lookups; the
    graph is small enough at typical pipeline sizes (hundreds of tables,
    thousands of edges max) that an index is overkill. If profiling shows
    these as a hotspot in S9, swap the implementation behind the same API.
    """

    edges: tuple[RelationshipEdge, ...]
    ordering: tuple[tuple[str, tuple[str, ...]], ...]

    def parents_of(
        self, child_table: str, child_columns: tuple[str, ...]
    ) -> tuple[RelationshipEdge, ...]:
        """Every edge whose child matches `(child_table, child_columns)`."""
        return tuple(
            e
            for e in self.edges
            if e.child_table == child_table and e.child_columns == child_columns
        )

    def children_of(
        self, parent_table: str, parent_columns: tuple[str, ...]
    ) -> tuple[RelationshipEdge, ...]:
        """Every edge whose parent matches `(parent_table, parent_columns)`."""
        return tuple(
            e
            for e in self.edges
            if e.parent_table == parent_table and e.parent_columns == parent_columns
        )


def build_relationship_graph(
    relationships: tuple[Relationship, ...],
    *,
    namespace_registry: NamespaceRegistry,
    orphan_policy_lookup: dict[tuple[str, tuple[str, ...]], OrphanPolicy],
) -> RelationshipGraph:
    """Build a `RelationshipGraph` from profile relationships + planner inputs.

    The function takes profile-side `Relationship` tuples (the source-data
    facts) plus two planner-side inputs the graph needs to resolve every
    edge to a `RelationshipEdge`:

    - `namespace_registry`: resolved by `build_namespace_registry`;
      every relationship's namespace is looked up via
      `registry.for_relationship(rel)` (raises if unresolvable).
    - `orphan_policy_lookup`: resolved by
      `check_orphan_fk_policy_completeness` (which validates the config
      side of the contract first) and keyed by
      `(parent_table, parent_columns)`. Every relationship key must be
      present in this lookup; the check is responsible for populating it.

    Raises:

    - `NamespaceConfigError(code='namespace_missing')`: a relationship
      cannot resolve a namespace via any of (relationship.namespace,
      parent-column binding, child-column binding).
    - `PlanCompileError(code='multi_parent_fk_unsupported')`: a child
      `(child_table, child_columns)` declares FK relationships to two or
      more distinct parent tables. Full multi-parent support is deferred
      to a future sprint per S2 spec TODO 3 resolution; this typed
      rejection prevents silent first-parent-wins.
    - `PlanCompileError(code='fk_cycle')`: the FK DAG contains a cycle.
      The error message lists the nodes participating in the cycle.

    Pure function: same `(relationships, namespace_registry,
    orphan_policy_lookup)` produces an equal graph.
    """
    # Multi-parent FK detection: group relationships by child key; reject
    # when two different relationships have the same child but different
    # parent tables.
    by_child: dict[tuple[str, tuple[str, ...]], list[Relationship]] = defaultdict(list)
    for rel in relationships:
        by_child[(rel.child_table, rel.child_columns)].append(rel)
    for (child_table, child_columns), rels in by_child.items():
        parent_tables = sorted({r.parent_table for r in rels})
        if len(parent_tables) > 1:
            raise PlanCompileError(
                code="multi_parent_fk_unsupported",
                path=f"relationships[{child_table}.{child_columns}]",
                message=(
                    f"Column {child_table}.{child_columns} declares FK relationships "
                    f"to multiple parent tables: {parent_tables!r}. Multi-parent FK "
                    "support is deferred to a future sprint; declare the column as "
                    "a FK to one parent only, or remove the redundant relationship "
                    "entries from the config."
                ),
            )

    # Resolve every relationship to a RelationshipEdge.
    edges: list[RelationshipEdge] = []
    for rel in relationships:
        namespace = namespace_registry.for_relationship(rel)
        parent_key = (rel.parent_table, rel.parent_columns)
        if parent_key not in orphan_policy_lookup:
            # Should not happen: check_orphan_fk_policy_completeness must
            # run before this function and must populate every key. If we
            # land here, it's a wiring bug in compile_plan, not user input.
            raise PlanCompileError(
                code="orphan_fk_policy_missing",
                path=(
                    f"relationships[{rel.parent_table}.{rel.parent_columns}->"
                    f"{rel.child_table}.{rel.child_columns}]"
                ),
                message=(
                    f"Relationship {rel.parent_table}.{rel.parent_columns} -> "
                    f"{rel.child_table}.{rel.child_columns} has no orphan_policy "
                    "in the lookup; check_orphan_fk_policy_completeness must run "
                    "before build_relationship_graph."
                ),
            )
        edges.append(
            RelationshipEdge(
                parent_table=rel.parent_table,
                parent_columns=rel.parent_columns,
                child_table=rel.child_table,
                child_columns=rel.child_columns,
                namespace=namespace,
                orphan_policy=orphan_policy_lookup[parent_key],
            )
        )

    # Sort edges deterministically for stable iteration.
    edges_sorted = tuple(
        sorted(
            edges,
            key=lambda e: (
                e.parent_table,
                e.parent_columns,
                e.child_table,
                e.child_columns,
            ),
        )
    )

    # Topological sort. Composite parent collapses to one node (the whole
    # column tuple). Kahn's algorithm with sorted-queue tie-breaking for
    # byte-stable ordering.
    nodes: set[tuple[str, tuple[str, ...]]] = set()
    for e in edges_sorted:
        nodes.add((e.parent_table, e.parent_columns))
        nodes.add((e.child_table, e.child_columns))

    indegree: dict[tuple[str, tuple[str, ...]], int] = dict.fromkeys(nodes, 0)
    out_edges: dict[tuple[str, tuple[str, ...]], list[tuple[str, tuple[str, ...]]]] = defaultdict(
        list
    )
    for e in edges_sorted:
        parent_node = (e.parent_table, e.parent_columns)
        child_node = (e.child_table, e.child_columns)
        out_edges[parent_node].append(child_node)
        indegree[child_node] += 1

    queue = sorted(n for n, d in indegree.items() if d == 0)
    ordered: list[tuple[str, tuple[str, ...]]] = []
    while queue:
        node = queue.pop(0)
        ordered.append(node)
        for nxt in sorted(out_edges[node]):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
        queue.sort()

    if len(ordered) != len(nodes):
        cycle_nodes = sorted(n for n, d in indegree.items() if d > 0)
        raise PlanCompileError(
            code="fk_cycle",
            path="relationships",
            message=(
                f"FK relationships form a cycle. {len(cycle_nodes)} nodes are part "
                f"of one or more cycles: {cycle_nodes!r}. Plan cannot determine a "
                "deterministic mask order."
            ),
        )

    return RelationshipGraph(edges=edges_sorted, ordering=tuple(ordered))


def check_orphan_fk_policy_completeness(
    config: dict[str, Any],
    relationships: tuple[Relationship, ...],
) -> dict[tuple[str, tuple[str, ...]], OrphanPolicy]:
    """Validate that the config declares `orphan_policy` for every relationship.

    Per S2 §3 (resolution of TODO 4): no default; every relationship must
    explicitly name its orphan-handling policy. Returns a lookup keyed
    by `(parent_table, parent_columns)` -> `OrphanPolicy` so
    `build_relationship_graph` can populate every `RelationshipEdge`
    without re-reading the config.

    Raises:

    - `PlanCompileError(code='orphan_fk_policy_missing')`: a relationship
      exists in `profile.relationships` but the config has no matching
      entry, OR the matching entry has no `orphan_policy` field.
    - `PlanCompileError(code='orphan_fk_policy_invalid')`: the config
      entry declares an `orphan_policy` value not in the four valid
      values (`preserve`, `remap`, `warn`, `fail`).

    Compile-check ownership table row #6 (per S1 spec).
    """
    # Build the lookup from the config side: (parent_table, parent_columns)
    # -> orphan_policy string. Reject invalid values immediately so the
    # error names the offending entry's exact location.
    config_lookup: dict[tuple[str, tuple[str, ...]], str] = {}
    config_relationships = config.get("relationships", [])
    if isinstance(config_relationships, list):
        for idx, entry in enumerate(config_relationships):
            if not isinstance(entry, dict):
                continue
            parent = entry.get("parent")
            if not isinstance(parent, dict):
                continue
            parent_table = parent.get("table")
            parent_cols = parent.get("columns")
            if not (
                isinstance(parent_table, str)
                and isinstance(parent_cols, list)
                and all(isinstance(c, str) for c in parent_cols)
            ):
                continue
            policy = entry.get("orphan_policy")
            if policy is None:
                raise PlanCompileError(
                    code="orphan_fk_policy_missing",
                    path=f"relationships[{idx}]",
                    message=(
                        f"Relationship entry for parent {parent_table}.{parent_cols} "
                        "does not declare an orphan_policy. Every relationship must "
                        "explicitly name one of: 'preserve', 'remap', 'warn', 'fail'. "
                        "There is no default."
                    ),
                )
            if policy not in ("preserve", "remap", "warn", "fail"):
                raise PlanCompileError(
                    code="orphan_fk_policy_invalid",
                    path=f"relationships[{idx}].orphan_policy",
                    message=(
                        f"Relationship entry for parent {parent_table}.{parent_cols} "
                        f"declares orphan_policy={policy!r}, which is not one of the "
                        "four valid values: 'preserve', 'remap', 'warn', 'fail'."
                    ),
                )
            config_lookup[(parent_table, tuple(parent_cols))] = policy

    # Now check every profile relationship has a matching config entry.
    lookup: dict[tuple[str, tuple[str, ...]], OrphanPolicy] = {}
    for rel in relationships:
        key = (rel.parent_table, rel.parent_columns)
        if key not in config_lookup:
            raise PlanCompileError(
                code="orphan_fk_policy_missing",
                path=(
                    f"relationships[{rel.parent_table}.{rel.parent_columns}->"
                    f"{rel.child_table}.{rel.child_columns}]"
                ),
                message=(
                    f"Profile declares relationship {rel.parent_table}."
                    f"{rel.parent_columns} -> {rel.child_table}.{rel.child_columns} "
                    "but the config has no matching relationship entry. Every "
                    "relationship requires an explicit orphan_policy declaration in "
                    "the config; one of: 'preserve', 'remap', 'warn', 'fail'."
                ),
            )
        lookup[key] = OrphanPolicy(config_lookup[key])
    return lookup

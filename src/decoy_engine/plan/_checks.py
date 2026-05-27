"""The five S1 plan-compile checks.

Each check is a pure function taking `(config, profile)` (and sometimes
additional precomputed state) and either returning silently on pass or
raising `PlanCompileError` on fail. The 5 checks are listed in the S1
spec compile-check ownership table rows 1-5.

S2+ adds more checks (orphan_fk_policy_completeness row 6,
pool_capacity_pre_flight row 7, etc.). S2's revisions of the planner
move some of these checks into `decoy_engine.relationships` (#1 namespace
ambiguity, #3 fk_plan_ordering); S1 keeps them inline.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.plan._registry_stub import S1_STUB_REGISTRY
from decoy_engine.profile._types import Profile

# Check 1 ----------------------------------------------------------------


def check_namespace_ambiguity(config: dict[str, Any]) -> None:
    """Reject configs where the same column is declared in two namespaces,
    or a deterministic-mode column is missing a namespace.

    Compile-check ownership table row #1; S2 moves into
    `decoy_engine.relationships._namespace.build_namespace_registry`.
    """
    column_to_namespaces: dict[tuple[str, str], set[str]] = defaultdict(set)
    namespaces = config.get("namespaces", {})
    if not isinstance(namespaces, dict):
        return  # malformed config; let YAML parsing catch it upstream
    for ns_name, ns_body in namespaces.items():
        declared_by = ns_body.get("declared_by", []) if isinstance(ns_body, dict) else []
        for entry in declared_by:
            if not isinstance(entry, str) or "." not in entry:
                continue
            table, col = entry.split(".", 1)
            column_to_namespaces[(table, col)].add(ns_name)

    for (table, col), ns_set in column_to_namespaces.items():
        if len(ns_set) > 1:
            raise PlanCompileError(
                code="namespace_ambiguity",
                path=f"namespaces.{{{','.join(sorted(ns_set))}}}.declared_by",
                message=(
                    f"Column {table}.{col} is declared in multiple namespaces: "
                    f"{sorted(ns_set)!r}. Each column may belong to exactly one namespace."
                ),
            )

    # Deterministic-mode columns must declare a namespace.
    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    for table_entry in tables:
        if not isinstance(table_entry, dict):
            continue
        table_name = table_entry.get("name", "?")
        for col_entry in table_entry.get("columns", []) or []:
            if not isinstance(col_entry, dict):
                continue
            mode = col_entry.get("cardinality_mode")
            if mode == "deterministic_map":
                col_name = col_entry.get("name", "?")
                if not col_entry.get("namespace"):
                    raise PlanCompileError(
                        code="namespace_ambiguity",
                        path=f"tables.{table_name}.columns.{col_name}",
                        message=(
                            f"Column {table_name}.{col_name} uses cardinality_mode "
                            "'deterministic_map' but does not declare a namespace. "
                            "Deterministic columns require an explicit namespace to "
                            "guarantee cross-column consistency."
                        ),
                    )


# Check 2 ----------------------------------------------------------------


def check_unknown_provider(config: dict[str, Any]) -> None:
    """Reject configs that reference a provider not in S1_STUB_REGISTRY.

    Compile-check ownership table row #2; S4 swaps the stub for the real
    registry behind the same check signature.
    """
    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    for table_entry in tables:
        if not isinstance(table_entry, dict):
            continue
        table_name = table_entry.get("name", "?")
        for col_entry in table_entry.get("columns", []) or []:
            if not isinstance(col_entry, dict):
                continue
            provider = col_entry.get("provider")
            if provider is None:
                continue
            if provider not in S1_STUB_REGISTRY:
                col_name = col_entry.get("name", "?")
                raise PlanCompileError(
                    code="unknown_provider",
                    path=f"tables.{table_name}.columns.{col_name}.provider",
                    message=(
                        f"Provider {provider!r} is not in S1_STUB_REGISTRY. "
                        f"Add it to decoy_engine.plan._registry_stub.S1_STUB_REGISTRY or "
                        "use one of the registered names. Custom providers land in S4."
                    ),
                )


# Check 3 ----------------------------------------------------------------


def check_fk_plan_ordering(profile: Profile) -> list[tuple[str, tuple[str, ...]]]:
    """Topologically sort the FK DAG; reject cycles.

    Returns the ordered list of (table, columns) nodes the planner uses
    to fill in `plan.ordering`. Composite parents are a single node;
    children come after every parent they depend on.

    Compile-check ownership table row #3; S2 moves into
    `decoy_engine.relationships._graph.build_relationship_graph`.
    """
    # Each node is (table, sorted-columns-tuple). Composite parents
    # collapse to one node by representing their whole column tuple as
    # a single ordering node.
    edges: list[tuple[tuple[str, tuple[str, ...]], tuple[str, tuple[str, ...]]]] = []
    nodes: set[tuple[str, tuple[str, ...]]] = set()
    for table in profile.tables:
        for col in table.columns:
            nodes.add((table.name, (col.name,)))
    for rel in profile.relationships:
        parent_node = (rel.parent_table, rel.parent_columns)
        child_node = (rel.child_table, rel.child_columns)
        nodes.add(parent_node)
        nodes.add(child_node)
        edges.append((parent_node, child_node))

    # Kahn's algorithm.
    indegree: dict[tuple[str, tuple[str, ...]], int] = dict.fromkeys(nodes, 0)
    out_edges: dict[tuple[str, tuple[str, ...]], list[tuple[str, tuple[str, ...]]]] = defaultdict(
        list
    )
    for p, c in edges:
        out_edges[p].append(c)
        indegree[c] += 1

    queue = sorted([n for n, d in indegree.items() if d == 0])
    ordered: list[tuple[str, tuple[str, ...]]] = []
    while queue:
        node = queue.pop(0)
        ordered.append(node)
        for nxt in sorted(out_edges[node]):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
        queue.sort()  # stability across implementations

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

    return ordered


# Check 4 ----------------------------------------------------------------


def check_basic_uniqueness_pre_flight(config: dict[str, Any], profile: Profile) -> None:
    """Reject pool-backed `unique` configs whose source distinct count
    exceeds the pool capacity hint.

    Partial in S1; S5 tightens with the full `pool_capacity_pre_flight`
    check. S1's check uses whatever capacity hint is available at compile
    time; if no hint is declared, the check passes (the runtime
    discovers the failure later).

    Compile-check ownership table row #4.
    """
    # Build a lookup from (table, column) -> ColumnProfile.distinct_count.
    distinct_lookup: dict[tuple[str, str], int | None] = {}
    for table in profile.tables:
        for col in table.columns:
            distinct_lookup[(table.name, col.name)] = col.distinct_count

    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    for table_entry in tables:
        if not isinstance(table_entry, dict):
            continue
        table_name = table_entry.get("name", "?")
        for col_entry in table_entry.get("columns", []) or []:
            if not isinstance(col_entry, dict):
                continue
            if col_entry.get("cardinality_mode") != "unique":
                continue
            if col_entry.get("backend_type") != "pool":
                continue
            pool_size = col_entry.get("pool_size")
            if pool_size is None:
                continue  # no static hint; runtime catches
            col_name = col_entry.get("name", "?")
            source_distinct = distinct_lookup.get((table_name, col_name))
            if source_distinct is None:
                continue  # profile lacks the column (sampled or absent)
            if source_distinct > pool_size:
                raise PlanCompileError(
                    code="pool_capacity_pre_flight_unique",
                    path=f"tables.{table_name}.columns.{col_name}",
                    message=(
                        f"Column {table_name}.{col_name} uses cardinality_mode=unique "
                        f"with pool_size={pool_size}, but the profile reports "
                        f"distinct_count={source_distinct} source rows. The pool "
                        "cannot supply enough unique values; raise pool_size or pick "
                        "a different cardinality_mode."
                    ),
                )


# Check 5 ----------------------------------------------------------------


def check_composite_columns_length_match(profile: Profile) -> None:
    """Every relationship's parent.columns and each child.columns must
    have the same length.

    Resolution of S2 spec B2 (composite-key shape). The Profile-layer
    Relationship dataclass enforces this at construction time; this
    check exists at the planner layer too so a Profile that was hand-
    constructed via dict (e.g. through deserialization without going
    through Relationship.__post_init__) gets caught here.

    Compile-check ownership table row #5.
    """
    for rel in profile.relationships:
        parent_len = len(rel.parent_columns)
        child_len = len(rel.child_columns)
        if parent_len != child_len:
            raise PlanCompileError(
                code="composite_columns_length_mismatch",
                path=(
                    f"relationships[{rel.parent_table}.{rel.parent_columns}->"
                    f"{rel.child_table}.{rel.child_columns}]"
                ),
                message=(
                    f"Relationship {rel.parent_table}.{rel.parent_columns} -> "
                    f"{rel.child_table}.{rel.child_columns}: parent columns length "
                    f"{parent_len} != child columns length {child_len}."
                ),
            )

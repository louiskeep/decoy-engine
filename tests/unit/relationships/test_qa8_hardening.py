"""QA-8 (2026-06-01) regression cells for relationships + namespace
hardening:

- F1: heapq-based Kahn topological sort produces byte-identical output
  to the prior list-based queue (same lexicographic order).
- F2: NamespaceRegistry._index pre-computes lookups; for_column is O(1).
- F3: duplicate-key orphan_policy with conflicting values raises
  orphan_fk_policy_duplicate (was silent last-wins).
"""

from __future__ import annotations

import pytest

from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.profile._types import Relationship
from decoy_engine.relationships._graph import (
    OrphanPolicy,
    build_relationship_graph,
    check_orphan_fk_policy_completeness,
)
from decoy_engine.relationships._namespace import (
    NamespaceBinding,
    NamespaceRegistry,
)


def _rel(parent_t: str, parent_c: str, child_t: str, child_c: str, *, ns: str | None = None) -> Relationship:
    return Relationship(
        parent_table=parent_t,
        parent_columns=(parent_c,),
        child_table=child_t,
        child_columns=(child_c,),
        namespace=ns,
    )


def _ns_registry_for(rels: list[Relationship]) -> NamespaceRegistry:
    """Build a minimal NamespaceRegistry for the given rels using
    table-name as the namespace placeholder."""
    bindings = tuple(
        NamespaceBinding(
            namespace=f"ns_{r.parent_table}",
            declared_by=(
                (r.parent_table, r.parent_columns),
                (r.child_table, r.child_columns),
            ),
        )
        for r in rels
    )
    return NamespaceRegistry(bindings=bindings)


def _orphan_lookup_for(rels: list[Relationship]) -> dict:
    # S13-rebaseline P1 (2026-06-01): lookup key now includes the child
    # end so per-(parent, child) policies are honored.
    return {
        (r.parent_table, r.parent_columns, r.child_table, r.child_columns): OrphanPolicy.PRESERVE
        for r in rels
    }


class TestQA8F1HeapqTopologicalSort:
    """F1: a chain graph topological-sort should produce the same
    lexicographic ordering under heapq as the original list-based queue."""

    def test_chain_topological_ordering_lexicographic(self):
        # 5-node chain: a -> b -> c -> d -> e
        rels = [
            _rel("a", "id", "b", "fk"),
            _rel("b", "id", "c", "fk"),
            _rel("c", "id", "d", "fk"),
            _rel("d", "id", "e", "fk"),
        ]
        graph = build_relationship_graph(
            tuple(rels),
            namespace_registry=_ns_registry_for(rels),
            orphan_policy_lookup=_orphan_lookup_for(rels),
        )
        order_tables = [n[0] for n in graph.ordering]
        for t in ["a", "b", "c", "d", "e"]:
            assert t in order_tables
        idx = {t: order_tables.index(t) for t in ["a", "b", "c", "d", "e"]}
        assert idx["a"] < idx["b"] < idx["c"] < idx["d"] < idx["e"]

    def test_independent_branches_lexicographic_tie_break(self):
        rels = [
            _rel("a", "id", "b", "fk"),
            _rel("c", "id", "d", "fk"),
        ]
        graph = build_relationship_graph(
            tuple(rels),
            namespace_registry=_ns_registry_for(rels),
            orphan_policy_lookup=_orphan_lookup_for(rels),
        )
        order = [n[0] for n in graph.ordering]
        assert order.index("a") < order.index("b")
        assert order.index("c") < order.index("d")
        assert order.index("a") < order.index("c")


class TestQA8F2NamespaceRegistryIndex:
    """F2: NamespaceRegistry._index is the O(1) lookup table; for_column
    returns the same answer as the pre-fix O(B*K) scan."""

    def test_for_column_returns_correct_namespace(self):
        bindings = (
            NamespaceBinding(
                namespace="patient_ns",
                declared_by=(("patients", ("ssn",)), ("appointments", ("patient_ssn",))),
            ),
            NamespaceBinding(
                namespace="provider_ns",
                declared_by=(("providers", ("npi",)),),
            ),
        )
        reg = NamespaceRegistry(bindings=bindings)
        assert reg.for_column("patients", ("ssn",)) == "patient_ns"
        assert reg.for_column("appointments", ("patient_ssn",)) == "patient_ns"
        assert reg.for_column("providers", ("npi",)) == "provider_ns"
        # Unbound column returns None.
        assert reg.for_column("orders", ("id",)) is None

    def test_for_column_index_rebuilt_in_post_init(self):
        """Constructing NamespaceRegistry(bindings=...) without an
        explicit _index still gets fast lookups: the dataclass
        __post_init__ rebuilds the index from bindings."""
        bindings = (
            NamespaceBinding(namespace="ns_a", declared_by=(("t1", ("c1",)),)),
        )
        reg = NamespaceRegistry(bindings=bindings)
        assert reg._index == {("t1", ("c1",)): "ns_a"}


class TestQA8F3OrphanPolicyDuplicate:
    """F3: a config with two entries for the same (parent_table,
    parent_cols) but different orphan_policy values now raises
    orphan_fk_policy_duplicate instead of silently last-winning."""

    def test_duplicate_conflicting_orphan_policy_raises(self):
        rels = (_rel("parent", "id", "child", "fk"),)
        config = {
            "relationships": [
                {
                    "parent": {"table": "parent", "columns": ["id"]},
                    "children": [{"table": "child", "columns": ["fk"]}],
                    "orphan_policy": "preserve",
                },
                {
                    "parent": {"table": "parent", "columns": ["id"]},
                    "children": [{"table": "child", "columns": ["fk"]}],
                    "orphan_policy": "fail",  # different policy on same key
                },
            ],
        }
        with pytest.raises(PlanCompileError) as exc:
            check_orphan_fk_policy_completeness(config, rels)
        assert exc.value.code == "orphan_fk_policy_duplicate"

    def test_duplicate_same_orphan_policy_passes(self):
        """Same-policy duplicate is tolerated (no information loss)."""
        rels = (_rel("parent", "id", "child", "fk"),)
        config = {
            "relationships": [
                {
                    "parent": {"table": "parent", "columns": ["id"]},
                    "children": [{"table": "child", "columns": ["fk"]}],
                    "orphan_policy": "preserve",
                },
                {
                    "parent": {"table": "parent", "columns": ["id"]},
                    "children": [{"table": "child", "columns": ["fk"]}],
                    "orphan_policy": "preserve",  # same policy
                },
            ],
        }
        # No raise.
        lookup = check_orphan_fk_policy_completeness(config, rels)
        # S13-rebaseline P1 (2026-06-01): lookup key shape now includes
        # child end.
        assert ("parent", ("id",), "child", ("fk",)) in lookup

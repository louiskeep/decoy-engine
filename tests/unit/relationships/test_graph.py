"""Unit tests for decoy_engine.relationships._graph.

Covers the S2 spec §Tests "Relationship graph" + "Multi-parent FK
rejection" + "Orphan policy check" blocks.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.profile import (
    Profile,
    Relationship,
)
from decoy_engine.relationships import (
    OrphanPolicy,
    RelationshipEdge,
    RelationshipGraph,
    build_namespace_registry,
    build_relationship_graph,
    check_orphan_fk_policy_completeness,
)


def _config_for(rel: Relationship, policy: str = "fail") -> dict:
    """Minimal config that declares an orphan_policy for one relationship."""
    return {
        "relationships": [
            {
                "parent": {
                    "table": rel.parent_table,
                    "columns": list(rel.parent_columns),
                },
                "children": [
                    {
                        "table": rel.child_table,
                        "columns": list(rel.child_columns),
                    }
                ],
                "orphan_policy": policy,
                "namespace": rel.namespace,
            }
        ]
    }


def _build_graph_for(
    profile: Profile,
    config: dict | None = None,
    policy: str = "fail",
) -> RelationshipGraph:
    """Helper: build registry + lookup + graph from a profile, defaulting all
    relationships to a single shared orphan_policy unless config overrides."""
    if config is None:
        config = {
            "relationships": [
                {
                    "parent": {
                        "table": rel.parent_table,
                        "columns": list(rel.parent_columns),
                    },
                    "children": [
                        {
                            "table": rel.child_table,
                            "columns": list(rel.child_columns),
                        }
                    ],
                    "orphan_policy": policy,
                    "namespace": rel.namespace,
                }
                for rel in profile.relationships
            ]
        }
    registry = build_namespace_registry(config, profile)
    lookup = check_orphan_fk_policy_completeness(config, profile.relationships)
    return build_relationship_graph(
        profile.relationships,
        namespace_registry=registry,
        orphan_policy_lookup=lookup,
    )


class TestOrdering:
    def test_parent_appears_before_child_in_ordering(self, parent_child_profile: Profile) -> None:
        graph = _build_graph_for(parent_child_profile)
        ordering = list(graph.ordering)
        parent_pos = ordering.index(("customers", ("customer_id",)))
        child_pos = ordering.index(("orders", ("customer_id",)))
        assert parent_pos < child_pos

    def test_fan_out_orders_parent_before_every_child(
        self, parent_three_children_profile: Profile
    ) -> None:
        graph = _build_graph_for(parent_three_children_profile)
        ordering = list(graph.ordering)
        parent_pos = ordering.index(("customers", ("customer_id",)))
        for child_table in ("orders", "invoices", "addresses"):
            child_pos = ordering.index((child_table, ("customer_id",)))
            assert parent_pos < child_pos, f"{child_table} ordered before customers (parent)"


class TestComposite:
    def test_composite_parent_is_one_ordering_node(self, composite_profile: Profile) -> None:
        graph = _build_graph_for(composite_profile)
        ordering = list(graph.ordering)
        # The whole composite tuple is one node, not three.
        assert ("enrollments", ("member_id", "plan_id", "effective_date")) in ordering
        assert ("enrollments", ("member_id",)) not in ordering
        assert ("enrollments", ("plan_id",)) not in ordering

    def test_composite_child_waits_on_whole_tuple(self, composite_profile: Profile) -> None:
        graph = _build_graph_for(composite_profile)
        ordering = list(graph.ordering)
        parent_pos = ordering.index(("enrollments", ("member_id", "plan_id", "effective_date")))
        child_pos = ordering.index(("claims", ("member_id", "plan_id", "effective_date")))
        assert parent_pos < child_pos


class TestCycleRejection:
    def test_simple_cycle_raises_fk_cycle(self) -> None:
        # a -> b and b -> a is a cycle.
        rels = (
            Relationship(
                parent_table="a",
                parent_columns=("id",),
                child_table="b",
                child_columns=("id",),
                namespace="ns",
            ),
            Relationship(
                parent_table="b",
                parent_columns=("id",),
                child_table="a",
                child_columns=("id",),
                namespace="ns",
            ),
        )
        profile = Profile(
            schema_version=1,
            tables=(),
            relationships=rels,
            profiled_at=datetime(2026, 5, 27, 0, 0, 0),
            decoy_engine_version="0.1.0",
        )
        with pytest.raises(PlanCompileError) as excinfo:
            _build_graph_for(profile)
        assert excinfo.value.code == "fk_cycle"

    def test_self_fk_same_column_raises_fk_cycle(self) -> None:
        """FC-2 G2: a self-FK where parent_column == child_column on the
        SAME table is a tautological cycle (the node depends on itself).
        Distinct columns on the same table (the canonical self-FK case)
        is two distinct topo nodes and DOES compile; same column on both
        sides collapses to one node with a self-edge, which the topo
        rejects as `fk_cycle`.

        Pin the current behavior: rejecting `parent_col == child_col` is
        correct (industry standard SQL semantics: `t.x -> t.x` has no
        meaningful resolution since there is nothing to point at), so
        future refactors cannot silently change it."""
        rels = (
            Relationship(
                parent_table="employees",
                parent_columns=("id",),
                child_table="employees",
                child_columns=("id",),
                namespace="employee_identity",
            ),
        )
        profile = Profile(
            schema_version=1,
            tables=(),
            relationships=rels,
            profiled_at=datetime(2026, 6, 2, 0, 0, 0),
            decoy_engine_version="0.1.0",
        )
        with pytest.raises(PlanCompileError) as excinfo:
            _build_graph_for(profile)
        assert excinfo.value.code == "fk_cycle"


class TestRoundTripQueries:
    def test_parents_of_and_children_of_round_trip(
        self, parent_three_children_profile: Profile
    ) -> None:
        graph = _build_graph_for(parent_three_children_profile)
        # Every edge appears in exactly one parents-of list and one
        # children-of list.
        for edge in graph.edges:
            parents = graph.parents_of(edge.child_table, edge.child_columns)
            children = graph.children_of(edge.parent_table, edge.parent_columns)
            assert edge in parents
            assert edge in children

    def test_parents_of_returns_empty_tuple_for_unknown_child(
        self, parent_child_profile: Profile
    ) -> None:
        graph = _build_graph_for(parent_child_profile)
        assert graph.parents_of("nowhere", ("nada",)) == ()

    def test_children_of_returns_empty_tuple_for_unknown_parent(
        self, parent_child_profile: Profile
    ) -> None:
        graph = _build_graph_for(parent_child_profile)
        assert graph.children_of("nowhere", ("nada",)) == ()


class TestMultiParentFkRejection:
    """S2 spec TODO 3 + H2 resolution: a child column declaring FK to
    multiple parent tables is rejected with code=multi_parent_fk_unsupported."""

    def test_two_parents_one_child_raises(self) -> None:
        rels = (
            Relationship(
                parent_table="parent_a",
                parent_columns=("id",),
                child_table="child",
                child_columns=("id",),
                namespace="shared_ns",
            ),
            Relationship(
                parent_table="parent_b",
                parent_columns=("id",),
                child_table="child",
                child_columns=("id",),
                namespace="shared_ns",
            ),
        )
        profile = Profile(
            schema_version=1,
            tables=(),
            relationships=rels,
            profiled_at=datetime(2026, 5, 27, 0, 0, 0),
            decoy_engine_version="0.1.0",
        )
        with pytest.raises(PlanCompileError) as excinfo:
            _build_graph_for(profile, policy="fail")
        assert excinfo.value.code == "multi_parent_fk_unsupported"

    def test_multi_parent_error_names_offending_tuple(self) -> None:
        rels = (
            Relationship(
                parent_table="parent_a",
                parent_columns=("id",),
                child_table="child",
                child_columns=("id",),
                namespace="shared_ns",
            ),
            Relationship(
                parent_table="parent_b",
                parent_columns=("id",),
                child_table="child",
                child_columns=("id",),
                namespace="shared_ns",
            ),
        )
        profile = Profile(
            schema_version=1,
            tables=(),
            relationships=rels,
            profiled_at=datetime(2026, 5, 27, 0, 0, 0),
            decoy_engine_version="0.1.0",
        )
        with pytest.raises(PlanCompileError) as excinfo:
            _build_graph_for(profile, policy="fail")
        msg = excinfo.value.message
        assert "parent_a" in msg and "parent_b" in msg
        assert "child" in msg


class TestOrphanPolicyCheck:
    def test_relationship_without_orphan_policy_raises_missing(
        self, parent_child_profile: Profile
    ) -> None:
        # Config declares the relationship but omits orphan_policy.
        config = {
            "relationships": [
                {
                    "parent": {"table": "customers", "columns": ["customer_id"]},
                    "children": [{"table": "orders", "columns": ["customer_id"]}],
                    # no orphan_policy
                    "namespace": "customer_identity",
                }
            ]
        }
        with pytest.raises(PlanCompileError) as excinfo:
            check_orphan_fk_policy_completeness(config, parent_child_profile.relationships)
        assert excinfo.value.code == "orphan_fk_policy_missing"

    def test_relationship_with_invalid_orphan_policy_raises_invalid(
        self, parent_child_profile: Profile
    ) -> None:
        config = {
            "relationships": [
                {
                    "parent": {"table": "customers", "columns": ["customer_id"]},
                    "children": [{"table": "orders", "columns": ["customer_id"]}],
                    "orphan_policy": "vaporize",
                    "namespace": "customer_identity",
                }
            ]
        }
        with pytest.raises(PlanCompileError) as excinfo:
            check_orphan_fk_policy_completeness(config, parent_child_profile.relationships)
        assert excinfo.value.code == "orphan_fk_policy_invalid"

    @pytest.mark.parametrize("policy", ["preserve", "remap", "warn", "fail"])
    def test_each_valid_orphan_policy_accepted(
        self, parent_child_profile: Profile, policy: str
    ) -> None:
        config = {
            "relationships": [
                {
                    "parent": {"table": "customers", "columns": ["customer_id"]},
                    "children": [{"table": "orders", "columns": ["customer_id"]}],
                    "orphan_policy": policy,
                    "namespace": "customer_identity",
                }
            ]
        }
        lookup = check_orphan_fk_policy_completeness(config, parent_child_profile.relationships)
        # S13-rebaseline P1 (2026-06-01): lookup is now keyed by
        # (parent_table, parent_cols, child_table, child_cols).
        assert lookup[
            ("customers", ("customer_id",), "orders", ("customer_id",))
        ] == OrphanPolicy(policy)

    def test_profile_relationship_without_matching_config_entry_raises(
        self, parent_child_profile: Profile
    ) -> None:
        # Profile has a relationship but config has no relationships block at all.
        config: dict = {}
        with pytest.raises(PlanCompileError) as excinfo:
            check_orphan_fk_policy_completeness(config, parent_child_profile.relationships)
        assert excinfo.value.code == "orphan_fk_policy_missing"


class TestEdgeShape:
    def test_edge_carries_resolved_namespace_and_policy(
        self, parent_child_profile: Profile
    ) -> None:
        graph = _build_graph_for(parent_child_profile, policy="warn")
        assert len(graph.edges) == 1
        edge = graph.edges[0]
        assert isinstance(edge, RelationshipEdge)
        assert edge.namespace == "customer_identity"
        assert edge.orphan_policy == OrphanPolicy.WARN

    def test_orphan_policy_enum_values_match_literal_strings(self) -> None:
        assert OrphanPolicy.PRESERVE.value == "preserve"
        assert OrphanPolicy.REMAP.value == "remap"
        assert OrphanPolicy.WARN.value == "warn"
        assert OrphanPolicy.FAIL.value == "fail"


class TestDeterminism:
    def test_two_builds_produce_equal_graphs(self, parent_three_children_profile: Profile) -> None:
        g1 = _build_graph_for(parent_three_children_profile)
        g2 = _build_graph_for(parent_three_children_profile)
        assert g1 == g2

    def test_graph_is_frozen(self, parent_child_profile: Profile) -> None:
        from dataclasses import FrozenInstanceError

        graph = _build_graph_for(parent_child_profile)
        with pytest.raises(FrozenInstanceError):
            graph.edges = ()  # type: ignore[misc]

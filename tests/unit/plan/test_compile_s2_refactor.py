"""S1 -> S2 regression parity tests (resolution of S2 spec B1 + M1).

The S2 refactor moves namespace_ambiguity + fk_plan_ordering checks into
`decoy_engine.relationships` and adds `orphan_fk_policy_completeness` at
row 6 of the compile-check ownership table. Per the B1 fix block, the
regression contract is a typed per-field assertion list, NOT a whole-
file byte-identical hash (the new check fires on every compile, so a
whole-file hash is unsatisfiable as worded).

This file enumerates the per-field assertions across representative
configs:

- `relationships[]` block: shape unchanged
- `namespaces[]` block: shape unchanged
- `ordering[]` block: shape unchanged
- `seed_envelope.per_column.namespace` values: equivalent to S1 inline path (M1)
- `seed_envelope.per_group.namespace` values: equivalent to S1 inline path (M1)
- `plan_compile.checks_passed`: equals S1's tuple + exactly one new entry
  (`orphan_fk_policy_completeness`) in the documented position (appended after row 5)

The wiring change also surfaces orphan_fk_policy_completeness as a new
required check; configs that previously compiled without an explicit
`orphan_policy` would now fail. Every fixture used in this regression
suite carries an explicit `orphan_policy` (the S1 stubs default to
'fail' or 'preserve' as needed).
"""

from __future__ import annotations

import pytest

from decoy_engine.plan import Plan, compile_plan
from decoy_engine.profile import Profile

# The exact checks_passed tuple S2 must emit. Documented position: row 6
# (`orphan_fk_policy_completeness`) appended after row 5
# (`composite_columns_length_match`). S1's order preserved.
EXPECTED_S2_CHECKS_PASSED = (
    "namespace_ambiguity",
    "unknown_provider",
    "fk_plan_ordering",
    "basic_uniqueness_pre_flight",
    "composite_columns_length_match",
    "orphan_fk_policy_completeness",
)


class TestChecksPassedShape:
    """B1 contract: checks_passed equals S1's tuple plus exactly one new
    entry (`orphan_fk_policy_completeness`), in the documented position."""

    def test_checks_passed_matches_expected_tuple(
        self, simple_config: dict, simple_profile: Profile
    ) -> None:
        plan = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")
        assert plan.plan_compile.checks_passed == EXPECTED_S2_CHECKS_PASSED

    def test_checks_passed_contains_exactly_six_entries(
        self, simple_config: dict, simple_profile: Profile
    ) -> None:
        plan = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")
        assert len(plan.plan_compile.checks_passed) == 6

    def test_orphan_fk_policy_completeness_at_documented_position(
        self, simple_config: dict, simple_profile: Profile
    ) -> None:
        """Row 6 of the compile-check ownership table is appended after row 5."""
        plan = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")
        assert plan.plan_compile.checks_passed[-1] == "orphan_fk_policy_completeness"
        assert plan.plan_compile.checks_passed[-2] == "composite_columns_length_match"

    def test_s1_check_order_preserved(self, simple_config: dict, simple_profile: Profile) -> None:
        plan = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")
        # S1's order: namespace_ambiguity, unknown_provider, fk_plan_ordering,
        # basic_uniqueness_pre_flight, composite_columns_length_match.
        assert plan.plan_compile.checks_passed[:5] == EXPECTED_S2_CHECKS_PASSED[:5]


class TestRelationshipsBlockShape:
    """Per-field assertion list: relationships[] block shape unchanged by
    the S2 refactor."""

    def test_relationships_block_has_one_entry_per_parent(
        self, simple_config: dict, simple_profile: Profile
    ) -> None:
        plan = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")
        assert len(plan.relationships) == 1
        rel = plan.relationships[0]
        assert rel.parent.table == "customers"
        assert rel.parent.columns == ("customer_id",)
        # children is a tuple of PlanRelationshipEnd; preserves S1 structure.
        assert len(rel.children) == 1
        assert rel.children[0].table == "orders"

    def test_relationship_carries_orphan_policy(
        self, simple_config: dict, simple_profile: Profile
    ) -> None:
        plan = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")
        assert plan.relationships[0].orphan_policy == "fail"

    def test_relationship_carries_namespace(
        self, simple_config: dict, simple_profile: Profile
    ) -> None:
        plan = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")
        assert plan.relationships[0].namespace == "customer_identity"


class TestNamespacesBlockShape:
    """Per-field assertion list: namespaces[] block shape unchanged."""

    def test_namespaces_block_contains_config_declared_namespaces(
        self, simple_config: dict, simple_profile: Profile
    ) -> None:
        plan = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")
        ns_names = {ns.namespace for ns in plan.namespaces}
        assert "customer_identity" in ns_names

    def test_namespace_block_carries_declared_by_entries(
        self, simple_config: dict, simple_profile: Profile
    ) -> None:
        plan = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")
        ns = next(n for n in plan.namespaces if n.namespace == "customer_identity")
        declared_tables = {table for (table, _) in ns.declared_by}
        assert "customers" in declared_tables
        assert "orders" in declared_tables


class TestOrderingBlockShape:
    def test_ordering_contains_parent_and_child_nodes(
        self, simple_config: dict, simple_profile: Profile
    ) -> None:
        plan = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")
        nodes = [(o.table, o.columns) for o in plan.ordering]
        assert ("customers", ("customer_id",)) in nodes
        assert ("orders", ("customer_id",)) in nodes

    def test_parent_orders_before_child(self, simple_config: dict, simple_profile: Profile) -> None:
        plan = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")
        nodes = [(o.table, o.columns) for o in plan.ordering]
        parent_pos = nodes.index(("customers", ("customer_id",)))
        child_pos = nodes.index(("orders", ("customer_id",)))
        assert parent_pos < child_pos


class TestSeedEnvelopeNamespaceEquivalence:
    """M1: per_column.namespace + per_group.namespace values produced by
    S2's registry-derived path equal what S1's inline path produced.

    Since both paths ultimately read the namespace from
    profile.relationships[i].namespace (the registry just wraps that
    lookup), values must match exactly."""

    def test_per_column_namespace_matches_config_declaration(self, simple_profile: Profile) -> None:
        # Config declares the customer-name column under namespace 'logins'.
        config = {
            "global_settings": {"seed": 1},
            "namespaces": {"logins": {"declared_by": ["customers.name"]}},
            "tables": [
                {
                    "name": "customers",
                    "columns": [
                        {
                            "name": "name",
                            "strategy": "faker_name",
                            "provider": "person_name",
                            "backend_type": "faker",
                            "backend_version": "stub-0",
                            "cardinality_mode": "reuse",
                            "namespace": "logins",
                        }
                    ],
                }
            ],
            "relationships": [
                {
                    "parent": {"table": "customers", "columns": ["customer_id"]},
                    "children": [{"table": "orders", "columns": ["customer_id"]}],
                    "orphan_policy": "fail",
                    "namespace": "customer_identity",
                }
            ],
        }
        plan = compile_plan(config, simple_profile, decoy_engine_version="0.1.0")
        per_table = dict(plan.seed_envelope.per_table)
        per_column = dict(per_table["customers"].per_column)
        assert per_column["name"].namespace == "logins"

    def test_per_group_namespace_matches_composite_relationship_namespace(
        self, composite_profile: Profile
    ) -> None:
        config = {
            "global_settings": {"seed": 1},
            "relationships": [
                {
                    "parent": {
                        "table": "enrollments",
                        "columns": ["member_id", "plan_id", "effective_date"],
                    },
                    "children": [
                        {
                            "table": "claims",
                            "columns": ["member_id", "plan_id", "effective_date"],
                        }
                    ],
                    "orphan_policy": "fail",
                    "namespace": "enrollment_identity",
                }
            ],
        }
        plan = compile_plan(config, composite_profile, decoy_engine_version="0.1.0")
        per_table = dict(plan.seed_envelope.per_table)
        per_group = dict(per_table["claims"].per_group)
        canonical_key = "effective_date__member_id__plan_id"
        assert per_group[canonical_key].namespace == "enrollment_identity"


class TestS2Determinism:
    """The S2 refactor preserves S1's determinism contract: two compiles
    on the same input produce equal Plans."""

    def test_two_compiles_produce_equal_relationship_graphs(
        self, simple_config: dict, simple_profile: Profile
    ) -> None:
        p1 = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")
        p2 = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")
        assert p1.relationships == p2.relationships
        assert p1.ordering == p2.ordering

    def test_two_compiles_produce_equal_namespace_registries(
        self, simple_config: dict, simple_profile: Profile
    ) -> None:
        p1 = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")
        p2 = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")
        assert p1.namespaces == p2.namespaces


class TestHashConfigExcludesSourcesAndTargets:
    """M1 from S1 end-of-sprint Dennis review (rolled into S2): the
    pipeline_config_hash must stay byte-identical across source/target
    binding swaps.

    Dennis lean: exclude `sources` and `targets` from _hash_config
    rather than document the gap. The advisory claim is now load-bearing:
    the same masking semantics hash to the same value regardless of how
    bytes are bound on the way in or out.
    """

    def test_swapping_source_binding_preserves_pipeline_config_hash(
        self, simple_config: dict, simple_profile: Profile
    ) -> None:
        config_with_file_source = dict(simple_config)
        config_with_file_source["sources"] = [
            {"name": "src", "type": "file", "path": "/tmp/in.csv"}
        ]
        config_with_s3_source = dict(simple_config)
        config_with_s3_source["sources"] = [
            {"name": "src", "type": "s3", "bucket": "b", "key": "in.csv"}
        ]
        p1 = compile_plan(config_with_file_source, simple_profile, decoy_engine_version="0.1.0")
        p2 = compile_plan(config_with_s3_source, simple_profile, decoy_engine_version="0.1.0")
        assert p1.pipeline_config_hash == p2.pipeline_config_hash

    def test_swapping_target_binding_preserves_pipeline_config_hash(
        self, simple_config: dict, simple_profile: Profile
    ) -> None:
        config_with_file_target = dict(simple_config)
        config_with_file_target["targets"] = [
            {"name": "tgt", "type": "file", "path": "/tmp/out.csv"}
        ]
        config_with_s3_target = dict(simple_config)
        config_with_s3_target["targets"] = [
            {"name": "tgt", "type": "s3", "bucket": "b", "key": "out.csv"}
        ]
        p1 = compile_plan(config_with_file_target, simple_profile, decoy_engine_version="0.1.0")
        p2 = compile_plan(config_with_s3_target, simple_profile, decoy_engine_version="0.1.0")
        assert p1.pipeline_config_hash == p2.pipeline_config_hash

    def test_changing_masking_semantics_does_change_hash(
        self, simple_config: dict, simple_profile: Profile
    ) -> None:
        """Sanity check: hash IS sensitive to masking-semantics changes."""
        p1 = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")
        modified = dict(simple_config)
        modified["global_settings"] = {"seed": 999}  # different seed
        p2 = compile_plan(modified, simple_profile, decoy_engine_version="0.1.0")
        assert p1.pipeline_config_hash != p2.pipeline_config_hash


class TestS2WiringInvariants:
    """Sanity checks specific to the S2 wiring: the planner delegates to
    the relationships module without changing observable behavior."""

    def test_compile_plan_returns_a_plan(
        self, simple_config: dict, simple_profile: Profile
    ) -> None:
        plan = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")
        assert isinstance(plan, Plan)

    def test_namespace_ambiguity_still_raises_via_registry_path(
        self, simple_profile: Profile
    ) -> None:
        """The S2 wiring relocates the check into build_namespace_registry
        but the user-visible behavior is unchanged: same code, same shape."""
        from decoy_engine.plan import PlanCompileError

        config = {
            "global_settings": {"seed": 1},
            "namespaces": {
                "a": {"declared_by": ["customers.customer_id"]},
                "b": {"declared_by": ["customers.customer_id"]},
            },
            "relationships": [
                {
                    "parent": {"table": "customers", "columns": ["customer_id"]},
                    "children": [{"table": "orders", "columns": ["customer_id"]}],
                    "orphan_policy": "fail",
                }
            ],
        }
        with pytest.raises(PlanCompileError) as excinfo:
            compile_plan(config, simple_profile, decoy_engine_version="0.1.0")
        assert excinfo.value.code == "namespace_ambiguity"

    def test_fk_cycle_still_raises_via_graph_path(self) -> None:
        """The S2 wiring relocates the cycle check into
        build_relationship_graph; surface unchanged."""
        from datetime import datetime

        from decoy_engine.plan import PlanCompileError
        from decoy_engine.profile import Profile, Relationship

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
        config = {
            "global_settings": {"seed": 1},
            "relationships": [
                {
                    "parent": {"table": "a", "columns": ["id"]},
                    "children": [{"table": "b", "columns": ["id"]}],
                    "orphan_policy": "fail",
                    "namespace": "ns",
                },
                {
                    "parent": {"table": "b", "columns": ["id"]},
                    "children": [{"table": "a", "columns": ["id"]}],
                    "orphan_policy": "fail",
                    "namespace": "ns",
                },
            ],
        }
        with pytest.raises(PlanCompileError) as excinfo:
            compile_plan(config, profile, decoy_engine_version="0.1.0")
        assert excinfo.value.code == "fk_cycle"

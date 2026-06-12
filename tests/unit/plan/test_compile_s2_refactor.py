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
# Post-S8: 9 entries (S5 added row 7; S6 added row 9; S8 added row 8
# composite_wiring_consistent, between row 7 and row 9). S2's regression
# contract (B1) holds: the S1+S2 prefix is unchanged, and post-S2
# sprints append in documented order.
EXPECTED_S2_CHECKS_PASSED = (
    "namespace_ambiguity",
    "unknown_provider",
    "fk_plan_ordering",
    "basic_uniqueness_pre_flight",
    "composite_columns_length_match",
    "orphan_fk_policy_completeness",
    "pool_capacity_pre_flight",
    "composite_wiring_consistent",
    "deterministic_namespace_completeness",
    # Row 10 (B1, S13): null-bearing-int reject rule.
    "null_bearing_int_unsupported",
    # Row 11 (audit H5, 2026-06-12): non-poolable provider on the pool
    # path rejected at compile time, appended at the tail.
    "non_poolable_provider_with_pool_backend",
    # Row 12 (capability-gaps WS3, 2026-06-12): statistical generate
    # columns vs their snapshot artifacts, appended at the tail.
    "statistical_columns",
)


class TestChecksPassedShape:
    """B1 contract: checks_passed equals S1's tuple plus exactly one new
    entry (`orphan_fk_policy_completeness`), in the documented position."""

    def test_checks_passed_matches_expected_tuple(
        self, simple_config: dict, simple_profile: Profile
    ) -> None:
        plan = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")
        assert plan.plan_compile.checks_passed == EXPECTED_S2_CHECKS_PASSED

    def test_checks_passed_contains_exactly_twelve_entries(
        self, simple_config: dict, simple_profile: Profile
    ) -> None:
        # 9 through S8, row 10 (B1/S13), row 11 (audit H5), row 12 (WS3).
        plan = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")
        assert len(plan.plan_compile.checks_passed) == 12

    def test_orphan_fk_policy_completeness_at_documented_position(
        self, simple_config: dict, simple_profile: Profile
    ) -> None:
        """Row 6 follows row 5; row 7 (pool_capacity_pre_flight) follows row 6
        post-S5; row 8 (composite_wiring_consistent) post-S8; row 9
        (deterministic_namespace_completeness) post-S6; row 10
        (null_bearing_int_unsupported) at the tail post-S13 (B1)."""
        plan = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")
        assert plan.plan_compile.checks_passed[-8] == "composite_columns_length_match"
        assert plan.plan_compile.checks_passed[-7] == "orphan_fk_policy_completeness"
        assert plan.plan_compile.checks_passed[-6] == "pool_capacity_pre_flight"
        assert plan.plan_compile.checks_passed[-5] == "composite_wiring_consistent"
        assert plan.plan_compile.checks_passed[-4] == "deterministic_namespace_completeness"
        assert plan.plan_compile.checks_passed[-3] == "null_bearing_int_unsupported"
        assert plan.plan_compile.checks_passed[-2] == "non_poolable_provider_with_pool_backend"
        assert plan.plan_compile.checks_passed[-1] == "statistical_columns"

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


class TestWalksGenF9HashConfigRaisesOnNonJsonNative:
    """QA walks/generators F9 (2026-06-01, LOW correctness):
    _hash_config no longer uses `default=str` in json.dumps. Pre-fix
    any non-JSON-native value (datetime, UUID, dataclass) was silently
    coerced to its str() repr, so two semantically different values
    that str-format identically produced the same config hash. Post-
    fix TypeError surfaces at plan-compile time."""

    def test_hash_config_raises_typeerror_on_datetime_in_config(self):
        from datetime import datetime as _dt

        from decoy_engine.plan._compile import _hash_config

        config_with_datetime = {
            "global_settings": {"seed": 1, "started_at": _dt(2026, 6, 1, 0, 0)},
        }
        with pytest.raises(TypeError):
            _hash_config(config_with_datetime)

    def test_hash_config_accepts_json_native_values(self):
        from decoy_engine.plan._compile import _hash_config

        config = {
            "global_settings": {"seed": 1, "name": "test"},
            "tables": [{"name": "t", "row_count": 5}],
        }
        # Must not raise; produces a deterministic hex digest.
        out = _hash_config(config)
        assert isinstance(out, str)
        assert len(out) == 64  # sha256 hex


class TestWalksGenF5BuildRelationshipsUsesPassedLookup:
    """QA walks/generators F5 (2026-06-01, MEDIUM design):
    _build_relationships consumes the orphan_policy_lookup that
    compile_plan already computed via check_orphan_fk_policy_completeness.
    Pre-fix the parse was duplicated; drift between the two parse paths
    could silently produce a Plan whose stamped policy disagreed with
    what the completeness check validated."""

    def test_build_relationships_uses_explicit_lookup_over_reparse(self):
        """When orphan_policy_lookup is passed explicitly, the function
        uses it directly; the config.relationships block is never
        re-parsed by _build_relationships."""
        from decoy_engine.plan._compile import _build_relationships
        from decoy_engine.profile import Profile
        from decoy_engine.profile._types import Relationship

        rel = Relationship(
            parent_table="a",
            parent_columns=("id",),
            child_table="b",
            child_columns=("a_id",),
            namespace=None,
        )
        profile = Profile(
            schema_version=1,
            tables=(),
            relationships=(rel,),
            profiled_at=__import__("datetime").datetime(2026, 6, 1),
            decoy_engine_version="0.1.0",
        )
        # Pass a lookup that says "fail" for (a, [id]) -> (b, [a_id]).
        # The config's relationships block is empty, so the OLD reparse
        # path would have used the "preserve" fallback. With F5 the
        # lookup wins. S13-rebaseline P1 (2026-06-01): key shape now
        # includes child end.
        config: dict = {"global_settings": {"seed": 1}, "relationships": []}
        lookup = {("a", ("id",), "b", ("a_id",)): "fail"}
        out = _build_relationships(config, profile, orphan_policy_lookup=lookup)
        assert len(out) == 1
        assert out[0].orphan_policy == "fail", (
            "QA walks/generators F5: _build_relationships must use the "
            "passed lookup, not re-parse config.relationships."
        )

    def test_build_relationships_falls_back_to_reparse_when_lookup_none(self):
        """Backwards-compat: callers that bypass compile_plan can still
        pass None (or omit the kwarg) and get the original parse path.
        The single in-tree caller (compile_plan) always passes the
        explicit lookup; this fallback is a defensive safety net."""
        from decoy_engine.plan._compile import _build_relationships
        from decoy_engine.profile import Profile
        from decoy_engine.profile._types import Relationship

        rel = Relationship(
            parent_table="a",
            parent_columns=("id",),
            child_table="b",
            child_columns=("a_id",),
            namespace=None,
        )
        profile = Profile(
            schema_version=1,
            tables=(),
            relationships=(rel,),
            profiled_at=__import__("datetime").datetime(2026, 6, 1),
            decoy_engine_version="0.1.0",
        )
        config = {
            "global_settings": {"seed": 1},
            "relationships": [
                {
                    "parent": {"table": "a", "columns": ["id"]},
                    "children": [{"table": "b", "columns": ["a_id"]}],
                    "orphan_policy": "fail",
                }
            ],
        }
        # No lookup passed: original parse path produces "fail".
        out = _build_relationships(config, profile)
        assert len(out) == 1
        assert out[0].orphan_policy == "fail"

"""Basic compile_plan tests: happy path + shape contract + determinism."""

from __future__ import annotations

import pytest

from decoy_engine.plan import (
    Plan,
    PlanCompileError,
    compile_plan,
    plan_from_yaml,
    plan_to_yaml,
)
from decoy_engine.profile import Profile


class TestCompilePlanHappyPath:
    def test_compile_produces_plan(self, simple_config: dict, simple_profile: Profile) -> None:
        plan = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")
        assert isinstance(plan, Plan)

    def test_compile_stamps_versions(self, simple_config: dict, simple_profile: Profile) -> None:
        """S3 bumped seed_protocol_version 0 -> 1; F-series 1 -> 2;
        QA walks/gen F3 2 -> 3; formula-hash 3 -> 4 (2026-06-01);
        WS1 FPE detokenization 4 -> 5 (2026-06-12)."""
        plan = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")
        assert plan.plan_version == 1
        assert plan.seed_protocol_version == 5
        assert plan.engine_version == "0.1.0"

    def test_compile_records_thirteen_checks_passed(
        self, simple_config: dict, simple_profile: Profile
    ) -> None:
        """S2 added orphan_fk_policy_completeness at row 6; S5 added
        pool_capacity_pre_flight at row 7; S8 added composite_wiring_consistent
        at row 8; S6/S7 added deterministic_namespace_completeness at row 9;
        S13 (B1) added null_bearing_int_unsupported at row 10; audit H5
        (2026-06-12) added non_poolable_provider_with_pool_backend at row 11;
        capability-gaps WS3/WS2 (2026-06-12) added statistical_columns at
        row 12 and text_redact_ner_available at row 13; the vault follow-up
        (2026-06-12) added vault_columns at row 14."""
        plan = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")
        assert set(plan.plan_compile.checks_passed) == {
            "namespace_ambiguity",
            "unknown_provider",
            "fk_plan_ordering",
            "basic_uniqueness_pre_flight",
            "composite_columns_length_match",
            "orphan_fk_policy_completeness",
            "pool_capacity_pre_flight",
            "composite_wiring_consistent",
            "deterministic_namespace_completeness",
            "null_bearing_int_unsupported",
            "non_poolable_provider_with_pool_backend",
            "statistical_columns",
            "text_redact_ner_available",
            "vault_columns",
        }

    def test_compile_no_warnings_no_errors_no_skipped(
        self, simple_config: dict, simple_profile: Profile
    ) -> None:
        plan = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")
        assert plan.plan_compile.warnings == ()
        assert plan.plan_compile.errors == ()
        assert plan.plan_compile.checks_skipped == ()


class TestCompileDeterminism:
    def test_two_compiles_equal(self, simple_config: dict, simple_profile: Profile) -> None:
        p1 = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")
        p2 = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")
        assert p1 == p2

    def test_two_compiles_yaml_byte_identical(
        self, simple_config: dict, simple_profile: Profile
    ) -> None:
        p1 = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")
        p2 = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")
        assert plan_to_yaml(p1) == plan_to_yaml(p2)

    def test_compile_pipeline_config_hash_is_deterministic(
        self, simple_config: dict, simple_profile: Profile
    ) -> None:
        # Hash should not depend on dict key insertion order.
        reordered_config = {
            "namespaces": simple_config["namespaces"],
            "relationships": simple_config["relationships"],
            "tables": simple_config["tables"],
            "global_settings": simple_config["global_settings"],
        }
        p1 = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")
        p2 = compile_plan(reordered_config, simple_profile, decoy_engine_version="0.1.0")
        assert p1.pipeline_config_hash == p2.pipeline_config_hash


class TestYamlRoundTrip:
    def test_round_trip_preserves_plan(self, simple_config: dict, simple_profile: Profile) -> None:
        plan = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")
        y = plan_to_yaml(plan)
        recovered = plan_from_yaml(y)
        assert recovered == plan

    def test_round_trip_yaml_is_valid_yaml(
        self, simple_config: dict, simple_profile: Profile
    ) -> None:
        plan = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")
        y = plan_to_yaml(plan)
        assert "plan_version: 1" in y
        assert "seed_protocol_version: 5" in y

    def test_yaml_emits_seed_protocol_version_five(
        self, simple_config: dict, simple_profile: Profile
    ) -> None:
        """The F-series corrections bump the stamped seed_protocol_version to
        2 (v1 = pre-correction era, v2 = corrected baseline)."""
        plan = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")
        y = plan_to_yaml(plan)
        assert "seed_protocol_version: 5" in y

    @pytest.mark.parametrize("policy", ["preserve", "remap", "warn", "fail"])
    def test_round_trip_preserves_each_orphan_policy(
        self, simple_profile: Profile, policy: str
    ) -> None:
        """M1 (Dennis slice 4-6 review): the YAML round-trip must cover all
        four valid OrphanPolicy values. Prior tests only exercised 'fail';
        a `_serialize.py` regression that mangles one of the others would
        land silently."""
        config: dict = {
            "global_settings": {"seed": 1},
            "relationships": [
                {
                    "parent": {"table": "customers", "columns": ["customer_id"]},
                    "children": [{"table": "orders", "columns": ["customer_id"]}],
                    "orphan_policy": policy,
                    "namespace": "customer_identity",
                }
            ],
        }
        plan = compile_plan(config, simple_profile, decoy_engine_version="0.1.0")
        # Confirm the planner picked up the policy from config.
        assert plan.relationships[0].orphan_policy == policy
        # Round-trip and assert equality.
        recovered = plan_from_yaml(plan_to_yaml(plan))
        assert recovered == plan
        assert recovered.relationships[0].orphan_policy == policy


class TestCompositeKeyHandling:
    def test_composite_relationship_in_plan(self, composite_profile: Profile) -> None:
        config: dict = {
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
        assert len(plan.relationships) == 1
        rel = plan.relationships[0]
        assert rel.parent.columns == ("member_id", "plan_id", "effective_date")
        assert rel.children[0].columns == ("member_id", "plan_id", "effective_date")

    def test_composite_parent_orders_before_child(self, composite_profile: Profile) -> None:
        # S2 requires orphan_policy on every relationship; previously this
        # test passed only global_settings because the orphan_fk_policy
        # check didn't exist yet.
        config: dict = {
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
        # Composite parent tuple appears as one ordering node; claims FK
        # nodes come after.
        ordering = list(plan.ordering)
        parent_pos = ordering.index(
            next(
                o
                for o in ordering
                if o.table == "enrollments"
                and o.columns == ("member_id", "plan_id", "effective_date")
            )
        )
        child_pos = ordering.index(
            next(
                o
                for o in ordering
                if o.table == "claims" and o.columns == ("member_id", "plan_id", "effective_date")
            )
        )
        assert parent_pos < child_pos

    def test_composite_emits_per_group_on_child_table(self, composite_profile: Profile) -> None:
        """M2 (Dennis slice 4-6 review): every composite relationship must
        produce a `per_group` entry on the CHILD table's TableSeed, keyed
        by the canonical-joined column name (sorted, "__"-joined). Per S1
        spec line 452."""
        config: dict = {
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
        # claims is the child table (composite FK to enrollments).
        assert "claims" in per_table, "expected per_table entry for claims (child table)"
        claims_per_group = dict(per_table["claims"].per_group)
        # Canonical key: sorted columns joined with "__".
        expected_key = "effective_date__member_id__plan_id"
        assert expected_key in claims_per_group, (
            f"expected per_group key {expected_key!r}, got keys: {list(claims_per_group)}"
        )
        gs = claims_per_group[expected_key]
        # GroupSeed preserves the declared child_columns order (for S8 to
        # match source-tuple positions).
        assert gs.coherent_columns == ("member_id", "plan_id", "effective_date")
        assert gs.namespace == "enrollment_identity"
        # Post-S3 plan-schema delta: GroupSeed no longer carries a
        # group_seed int field; deterministic material lives in
        # decoy_engine.determinism.derive(...) and is keyed by namespace
        # string + canonical-tuple source bytes. The S1 assertion
        # `gs.group_seed != 0` no longer applies.

    def test_composite_skips_per_column_for_member_columns_on_child(
        self, composite_profile: Profile
    ) -> None:
        """M2: per_column on the child table must NOT duplicate composite-
        member columns. Per S1 spec line 452."""
        config = {
            "global_settings": {"seed": 1},
            "tables": [
                {
                    "name": "claims",
                    "columns": [
                        # Declare strategies for both composite-member columns
                        # AND a non-member column. Only the non-member should
                        # appear in per_column.
                        {
                            "name": "member_id",
                            "strategy": "stub",
                            "provider": "synthetic_member_id",
                        },
                        {
                            "name": "claim_id",
                            "strategy": "stub",
                            "provider": "uuid",
                        },
                    ],
                }
            ],
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
        claims_per_column = dict(per_table["claims"].per_column)
        # member_id is a composite-member of the claims FK; it must not be in per_column.
        assert "member_id" not in claims_per_column
        # claim_id is not a composite member; per_column entry survives.
        assert "claim_id" in claims_per_column

    def test_composite_emits_per_group_even_when_config_has_no_tables_entry(
        self, composite_profile: Profile
    ) -> None:
        """M2 corollary: a table can have a per_group entry even with no
        config-declared strategies, because the composite relationship is
        a structural fact from the profile, not from the config."""
        # No `tables` key in config; just the relationship + orphan_policy.
        plan = compile_plan(
            {
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
            },
            composite_profile,
            decoy_engine_version="0.1.0",
        )
        per_table = dict(plan.seed_envelope.per_table)
        assert "claims" in per_table
        assert len(per_table["claims"].per_group) == 1
        # No per_column because the config declared nothing.
        assert per_table["claims"].per_column == ()


class TestErrorShape:
    def test_error_carries_code_path_message(self) -> None:
        e = PlanCompileError(code="x", path="a.b.c", message="m")
        assert e.code == "x"
        assert e.path == "a.b.c"
        assert e.message == "m"
        assert "[x]" in str(e)
        assert "a.b.c" in str(e)

    def test_error_with_none_path(self) -> None:
        e = PlanCompileError(code="x", path=None, message="m")
        assert e.path is None
        assert "[x]" in str(e)

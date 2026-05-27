"""Basic compile_plan tests: happy path + shape contract + determinism."""

from __future__ import annotations

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
        plan = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")
        assert plan.plan_version == 1
        assert plan.seed_protocol_version == 0
        assert plan.engine_version == "0.1.0"

    def test_compile_records_five_checks_passed(
        self, simple_config: dict, simple_profile: Profile
    ) -> None:
        plan = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")
        assert set(plan.plan_compile.checks_passed) == {
            "namespace_ambiguity",
            "unknown_provider",
            "fk_plan_ordering",
            "basic_uniqueness_pre_flight",
            "composite_columns_length_match",
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
        assert "seed_protocol_version: 0" in y

    def test_yaml_emits_seed_protocol_version_zero(
        self, simple_config: dict, simple_profile: Profile
    ) -> None:
        """S1 spec H1: seed_protocol_version stamp is `0` in S1 emission."""
        plan = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")
        y = plan_to_yaml(plan)
        assert "seed_protocol_version: 0" in y


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
        config: dict = {"global_settings": {"seed": 1}}
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


class TestGlobalSettingsEdgeCases:
    """M3 regression tests (Dennis session 15): global_settings null/missing edge cases."""

    def test_null_global_settings_does_not_raise(self, simple_profile: Profile) -> None:
        """global_settings: null (explicit YAML null) must not raise AttributeError."""
        config = {"global_settings": None}
        plan = compile_plan(config, simple_profile, decoy_engine_version="0.1.0")
        assert plan.seed_envelope.job_seed == 0

    def test_missing_global_settings_uses_zero_seed(self, simple_profile: Profile) -> None:
        """global_settings key absent entirely yields seed=0."""
        config: dict = {}
        plan = compile_plan(config, simple_profile, decoy_engine_version="0.1.0")
        assert plan.seed_envelope.job_seed == 0

    def test_explicit_seed_propagates(self, simple_profile: Profile) -> None:
        """global_settings.seed is picked up when present."""
        config = {"global_settings": {"seed": 99}}
        plan = compile_plan(config, simple_profile, decoy_engine_version="0.1.0")
        assert plan.seed_envelope.job_seed != 0  # derived from 99; just not zero


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

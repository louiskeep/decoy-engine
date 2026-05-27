"""S4 H1 planner-stamping integration tests.

Per S4 spec §9 + cold-read H1 PO call: the planner consults the registry
for `backend_type` + `backend_version`; user YAML overrides are ignored
with a `backend_stamp_user_override_ignored` warning on
`PlanCompileResult.warnings`.
"""

from __future__ import annotations

import faker as faker_module

from decoy_engine.plan import compile_plan
from decoy_engine.profile import Profile


class TestRegistryStamps:
    def test_backend_type_from_registry(self, simple_config: dict, simple_profile: Profile) -> None:
        plan = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")
        per_table = dict(plan.seed_envelope.per_table)
        per_column = dict(per_table["customers"].per_column)
        # `name` is the column with strategy in simple_config; its provider
        # is "person_name" which the registry binds to FakerAdapter.
        assert per_column["name"].backend_type == "faker"

    def test_backend_version_from_registry(
        self, simple_config: dict, simple_profile: Profile
    ) -> None:
        plan = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")
        per_table = dict(plan.seed_envelope.per_table)
        per_column = dict(per_table["customers"].per_column)
        # H1: registry stamps the real faker version, not the "stub-0" S1 default.
        assert per_column["name"].backend_version == faker_module.VERSION
        assert per_column["name"].backend_version != "stub-0"


class TestUserOverrideWarning:
    """User-supplied backend_type / backend_version that contradict the
    registry: warning emitted on PlanCompileResult.warnings; registry wins."""

    def test_user_backend_type_override_emits_warning(self, simple_profile: Profile) -> None:
        config = {
            "global_settings": {"seed": 1},
            "tables": [
                {
                    "name": "customers",
                    "columns": [
                        {
                            "name": "name",
                            "strategy": "faker_name",
                            "provider": "person_name",
                            "backend_type": "mimesis",  # contradicts registry
                            "cardinality_mode": "reuse",
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
        assert any(
            "backend_stamp_user_override_ignored" in w and "backend_type" in w
            for w in plan.plan_compile.warnings
        )
        # Registry still wins on the stamp.
        per_table = dict(plan.seed_envelope.per_table)
        per_column = dict(per_table["customers"].per_column)
        assert per_column["name"].backend_type == "faker"

    def test_user_backend_version_override_emits_warning(self, simple_profile: Profile) -> None:
        config = {
            "global_settings": {"seed": 1},
            "tables": [
                {
                    "name": "customers",
                    "columns": [
                        {
                            "name": "name",
                            "strategy": "faker_name",
                            "provider": "person_name",
                            "backend_version": "0.0.0-fake",
                            "cardinality_mode": "reuse",
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
        assert any(
            "backend_stamp_user_override_ignored" in w and "backend_version" in w
            for w in plan.plan_compile.warnings
        )
        per_table = dict(plan.seed_envelope.per_table)
        per_column = dict(per_table["customers"].per_column)
        assert per_column["name"].backend_version == faker_module.VERSION

    def test_no_warning_when_user_matches_registry(self, simple_profile: Profile) -> None:
        config = {
            "global_settings": {"seed": 1},
            "tables": [
                {
                    "name": "customers",
                    "columns": [
                        {
                            "name": "name",
                            "strategy": "faker_name",
                            "provider": "person_name",
                            "backend_type": "faker",  # matches registry
                            "backend_version": faker_module.VERSION,  # matches registry
                            "cardinality_mode": "reuse",
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
        # No backend_stamp warnings when user value matches registry.
        assert not any(
            "backend_stamp_user_override_ignored" in w for w in plan.plan_compile.warnings
        )


class TestUnknownProviderViaRegistry:
    """check_unknown_provider was swapped to consult the registry; behavior
    contract preserved (same configs rejected, same configs accepted)."""

    def test_unknown_provider_rejected(self, simple_profile: Profile) -> None:
        import pytest

        from decoy_engine.plan import PlanCompileError

        config = {
            "global_settings": {"seed": 1},
            "tables": [
                {
                    "name": "customers",
                    "columns": [
                        {
                            "name": "name",
                            "strategy": "x",
                            "provider": "not_a_real_provider",
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
        with pytest.raises(PlanCompileError) as excinfo:
            compile_plan(config, simple_profile, decoy_engine_version="0.1.0")
        assert excinfo.value.code == "unknown_provider"
        assert "not_a_real_provider" in excinfo.value.message

    def test_s4_added_providers_accepted(self, simple_profile: Profile) -> None:
        """S4 added 4 new names (synthetic_npi, synthetic_ndc, synthetic_mrn,
        person_full_name); they should all compile cleanly."""
        for new_provider in (
            "synthetic_npi",
            "synthetic_ndc",
            "synthetic_mrn",
            "person_full_name",
        ):
            config = {
                "global_settings": {"seed": 1},
                "tables": [
                    {
                        "name": "customers",
                        "columns": [
                            {
                                "name": "name",
                                "strategy": "faker_name",
                                "provider": new_provider,
                                "cardinality_mode": "reuse",
                            }
                        ],
                    }
                ],
                "relationships": [
                    {
                        "parent": {
                            "table": "customers",
                            "columns": ["customer_id"],
                        },
                        "children": [{"table": "orders", "columns": ["customer_id"]}],
                        "orphan_policy": "fail",
                        "namespace": "customer_identity",
                    }
                ],
            }
            plan = compile_plan(config, simple_profile, decoy_engine_version="0.1.0")
            per_table = dict(plan.seed_envelope.per_table)
            per_column = dict(per_table["customers"].per_column)
            assert per_column["name"].provider == new_provider

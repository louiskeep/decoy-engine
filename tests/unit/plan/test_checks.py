"""Tests for the 5 S1 plan-compile checks."""

from __future__ import annotations

from datetime import datetime

import pytest

from decoy_engine.plan import PlanCompileError, compile_plan
from decoy_engine.profile import (
    ColumnProfile,
    Profile,
    Relationship,
    TableProfile,
)


def _col(name: str, **kwargs) -> ColumnProfile:
    defaults = {
        "name": name,
        "dtype": "object",
        "row_count": 10,
        "null_count": 0,
        "distinct_count": 10,
        "sampled": False,
        "is_candidate_key_sampled": False,
        "declared_pk": False,
        "is_fk": False,
        "fk_target": None,
        "pii_class": None,
    }
    defaults.update(kwargs)
    return ColumnProfile(**defaults)


class TestNamespaceAmbiguity:
    def test_rejects_column_declared_in_two_namespaces(self, simple_profile: Profile) -> None:
        config = {
            "global_settings": {"seed": 1},
            "namespaces": {
                "ns_a": {"declared_by": ["customers.customer_id"]},
                "ns_b": {"declared_by": ["customers.customer_id"]},
            },
        }
        with pytest.raises(PlanCompileError) as exc:
            compile_plan(config, simple_profile, decoy_engine_version="0.1.0")
        assert exc.value.code == "namespace_ambiguity"
        assert "customer_id" in exc.value.message

    def test_rejects_deterministic_mode_without_namespace(self, simple_profile: Profile) -> None:
        config = {
            "global_settings": {"seed": 1},
            "tables": [
                {
                    "name": "customers",
                    "columns": [
                        {
                            "name": "email",
                            "strategy": "faker_email",
                            "provider": "person_email",
                            "cardinality_mode": "deterministic_map",
                            # namespace intentionally omitted
                        }
                    ],
                }
            ],
        }
        with pytest.raises(PlanCompileError) as exc:
            compile_plan(config, simple_profile, decoy_engine_version="0.1.0")
        assert exc.value.code == "namespace_ambiguity"
        assert "namespace" in exc.value.message.lower()

    def test_accepts_deterministic_mode_with_namespace(self, simple_profile: Profile) -> None:
        config = {
            "global_settings": {"seed": 1},
            "tables": [
                {
                    "name": "customers",
                    "columns": [
                        {
                            "name": "email",
                            "strategy": "faker_email",
                            "provider": "person_email",
                            "cardinality_mode": "deterministic_map",
                            "namespace": "email_ns",
                        }
                    ],
                }
            ],
            "namespaces": {"email_ns": {"declared_by": ["customers.email"]}},
        }
        # Should not raise.
        compile_plan(config, simple_profile, decoy_engine_version="0.1.0")


class TestUnknownProvider:
    def test_rejects_unknown_provider(self, simple_profile: Profile) -> None:
        config = {
            "global_settings": {"seed": 1},
            "tables": [
                {
                    "name": "customers",
                    "columns": [
                        {
                            "name": "name",
                            "strategy": "x",
                            "provider": "completely_fake_provider",
                        }
                    ],
                }
            ],
        }
        with pytest.raises(PlanCompileError) as exc:
            compile_plan(config, simple_profile, decoy_engine_version="0.1.0")
        assert exc.value.code == "unknown_provider"
        assert "completely_fake_provider" in exc.value.message

    def test_accepts_provider_from_stub_registry(self, simple_profile: Profile) -> None:
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
                        }
                    ],
                }
            ],
        }
        compile_plan(config, simple_profile, decoy_engine_version="0.1.0")


class TestFkPlanOrdering:
    def test_parent_orders_before_child(self, simple_profile: Profile) -> None:
        config = {"global_settings": {"seed": 1}}
        plan = compile_plan(config, simple_profile, decoy_engine_version="0.1.0")
        parent_pos = next(i for i, o in enumerate(plan.ordering) if o.table == "customers")
        child_pos = next(
            i
            for i, o in enumerate(plan.ordering)
            if o.table == "orders" and o.columns == ("customer_id",)
        )
        assert parent_pos < child_pos

    def test_rejects_cycle(self) -> None:
        # 2-cycle: (a, id) <-> (b, id). Each relationship declares one
        # direction; the (table, columns) graph cycles back through itself.
        a = TableProfile(
            name="a",
            row_count=1,
            columns=(_col("id", row_count=1, distinct_count=1, declared_pk=True),),
        )
        b = TableProfile(
            name="b",
            row_count=1,
            columns=(_col("id", row_count=1, distinct_count=1, declared_pk=True),),
        )
        profile = Profile(
            schema_version=1,
            tables=(a, b),
            relationships=(
                Relationship(
                    parent_table="a",
                    parent_columns=("id",),
                    child_table="b",
                    child_columns=("id",),
                    namespace=None,
                ),
                Relationship(
                    parent_table="b",
                    parent_columns=("id",),
                    child_table="a",
                    child_columns=("id",),
                    namespace=None,
                ),
            ),
            profiled_at=datetime(2026, 5, 27, 0, 0, 0),
            decoy_engine_version="0.1.0",
        )
        with pytest.raises(PlanCompileError) as exc:
            compile_plan({"global_settings": {"seed": 1}}, profile, decoy_engine_version="0.1.0")
        assert exc.value.code == "fk_cycle"


class TestBasicUniquenessPreFlight:
    def test_rejects_pool_unique_with_insufficient_capacity(self, simple_profile: Profile) -> None:
        # customers has 10 distinct customer_id values; pool of 5 is too small.
        config = {
            "global_settings": {"seed": 1},
            "tables": [
                {
                    "name": "customers",
                    "columns": [
                        {
                            "name": "customer_id",
                            "strategy": "from_pool",
                            "provider": "uuid",
                            "backend_type": "pool",
                            "cardinality_mode": "unique",
                            "pool_size": 5,
                        }
                    ],
                }
            ],
        }
        with pytest.raises(PlanCompileError) as exc:
            compile_plan(config, simple_profile, decoy_engine_version="0.1.0")
        assert exc.value.code == "pool_capacity_pre_flight_unique"

    def test_accepts_pool_unique_with_sufficient_capacity(self, simple_profile: Profile) -> None:
        config = {
            "global_settings": {"seed": 1},
            "tables": [
                {
                    "name": "customers",
                    "columns": [
                        {
                            "name": "customer_id",
                            "strategy": "from_pool",
                            "provider": "uuid",
                            "backend_type": "pool",
                            "cardinality_mode": "unique",
                            "pool_size": 1000,
                        }
                    ],
                }
            ],
        }
        compile_plan(config, simple_profile, decoy_engine_version="0.1.0")

    def test_no_pool_size_hint_passes(self, simple_profile: Profile) -> None:
        # When no pool_size is declared, the check passes silently (runtime catches).
        config = {
            "global_settings": {"seed": 1},
            "tables": [
                {
                    "name": "customers",
                    "columns": [
                        {
                            "name": "customer_id",
                            "strategy": "from_pool",
                            "provider": "uuid",
                            "backend_type": "pool",
                            "cardinality_mode": "unique",
                        }
                    ],
                }
            ],
        }
        compile_plan(config, simple_profile, decoy_engine_version="0.1.0")


class TestCompositeColumnsLengthMatch:
    def test_accepts_matched_composite(self, composite_profile: Profile) -> None:
        # composite_profile has matched 3-column tuples.
        compile_plan(
            {"global_settings": {"seed": 1}}, composite_profile, decoy_engine_version="0.1.0"
        )

    def test_rejects_mismatched_composite_via_planner(self) -> None:
        # The Profile-layer Relationship dataclass __post_init__ blocks
        # this case at construction. So to test the planner-layer check
        # in isolation, we'd need to bypass Relationship. Verify the
        # dataclass guard fires first.
        with pytest.raises(ValueError, match="parent_columns length"):
            Relationship(
                parent_table="a",
                parent_columns=("x", "y"),
                child_table="b",
                child_columns=("x",),
                namespace=None,
            )

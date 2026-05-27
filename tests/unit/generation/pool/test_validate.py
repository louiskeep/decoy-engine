"""pool_capacity_pre_flight + R6 reshape integration tests (S5 spec §6)."""

from __future__ import annotations

from datetime import datetime

import pytest

from decoy_engine.generation.pool import PoolCapacityError
from decoy_engine.plan import PlanCompileError, compile_plan
from decoy_engine.profile import (
    ColumnProfile,
    Profile,
    Relationship,
    TableProfile,
)


def _profile_with_distinct(table: str, col: str, distinct: int) -> Profile:
    cp = ColumnProfile(
        name=col,
        dtype="object",
        row_count=distinct * 10,
        null_count=0,
        distinct_count=distinct,
        sampled=False,
        is_candidate_key_sampled=False,
        declared_pk=False,
        is_fk=False,
        fk_target=None,
        pii_class=None,
    )
    customers_id = ColumnProfile(
        name="customer_id",
        dtype="object",
        row_count=distinct,
        null_count=0,
        distinct_count=distinct,
        sampled=False,
        is_candidate_key_sampled=True,
        declared_pk=True,
        is_fk=False,
        fk_target=None,
        pii_class=None,
    )
    return Profile(
        schema_version=1,
        tables=(
            TableProfile(name="customers", row_count=distinct, columns=(customers_id, cp)),
            TableProfile(
                name="orders",
                row_count=distinct,
                columns=(
                    ColumnProfile(
                        name="customer_id",
                        dtype="object",
                        row_count=distinct,
                        null_count=0,
                        distinct_count=distinct,
                        sampled=False,
                        is_candidate_key_sampled=False,
                        declared_pk=False,
                        is_fk=True,
                        fk_target=("customers", "customer_id"),
                        pii_class=None,
                    ),
                ),
            ),
        ),
        relationships=(
            Relationship(
                parent_table="customers",
                parent_columns=("customer_id",),
                child_table="orders",
                child_columns=("customer_id",),
                namespace="customer_identity",
            ),
        ),
        profiled_at=datetime(2026, 5, 27, 0, 0, 0),
        decoy_engine_version="0.1.0",
    )


def _config(cardinality: str, pool_size: int) -> dict:
    return {
        "global_settings": {"seed": 1, "on_pool_exhaustion": "fail"},
        "tables": [
            {
                "name": "customers",
                "columns": [
                    {
                        "name": "email",
                        "strategy": "faker_email",
                        "provider": "person_email",
                        "cardinality_mode": cardinality,
                        "pool_size": pool_size,
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


class TestPoolCapacityPreFlight:
    def test_unique_pool_too_small_raises_with_fail_mode(self) -> None:
        """PoolCapacityError is a peer of PlanCompileError per the S5 spec
        exception hierarchy; compile_plan surfaces it as-is."""
        with pytest.raises(PoolCapacityError) as excinfo:
            compile_plan(
                _config("unique", pool_size=10),
                _profile_with_distinct("customers", "email", 50),
                decoy_engine_version="0.1.0",
            )
        assert excinfo.value.code == "pool_too_small_for_source"

    def test_unique_pool_large_enough_passes(self) -> None:
        plan = compile_plan(
            _config("unique", pool_size=200),
            _profile_with_distinct("customers", "email", 50),
            decoy_engine_version="0.1.0",
        )
        assert plan is not None

    def test_scale_up_default_does_not_raise(self) -> None:
        """on_pool_exhaustion default is scale_up (PO PQ3); the check
        defers to runtime. Plan-compile does NOT raise."""
        config = _config("unique", pool_size=10)
        config["global_settings"]["on_pool_exhaustion"] = "scale_up"
        plan = compile_plan(
            config,
            _profile_with_distinct("customers", "email", 50),
            decoy_engine_version="0.1.0",
        )
        assert plan is not None

    def test_reuse_mode_skips_capacity_check(self) -> None:
        """REUSE doesn't need capacity guarantees; check is skipped even
        with pool_size < source distinct."""
        plan = compile_plan(
            _config("reuse", pool_size=10),
            _profile_with_distinct("customers", "email", 50),
            decoy_engine_version="0.1.0",
        )
        assert plan is not None


class TestR6PlanCompileSchema:
    """R6 reshape: deterministic_map -> deterministic: bool + cardinality_mode."""

    def test_deterministic_map_raises_rename_error(self) -> None:
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
                            "cardinality_mode": "deterministic_map",  # legacy
                            "namespace": "ns",
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
            compile_plan(
                config,
                _profile_with_distinct("customers", "email", 10),
                decoy_engine_version="0.1.0",
            )
        assert excinfo.value.code == "plan_schema_deterministic_map_renamed"
        # Migration instructions in the message.
        assert "deterministic: true" in excinfo.value.message
        assert "cardinality_mode" in excinfo.value.message

    def test_deterministic_field_defaults_to_false(self) -> None:
        """Plan-compile accepts configs that omit `deterministic:`; it
        defaults to False."""
        config = _config("reuse", pool_size=100)
        plan = compile_plan(
            config,
            _profile_with_distinct("customers", "email", 10),
            decoy_engine_version="0.1.0",
        )
        per_table = dict(plan.seed_envelope.per_table)
        per_column = dict(per_table["customers"].per_column)
        assert per_column["email"].deterministic is False

    def test_deterministic_field_read_from_yaml(self) -> None:
        config = _config("reuse", pool_size=100)
        config["tables"][0]["columns"][0]["deterministic"] = True
        config["tables"][0]["columns"][0]["namespace"] = "ns_email"
        config["namespaces"] = {"ns_email": {"declared_by": ["customers.email"]}}
        plan = compile_plan(
            config,
            _profile_with_distinct("customers", "email", 10),
            decoy_engine_version="0.1.0",
        )
        per_table = dict(plan.seed_envelope.per_table)
        per_column = dict(per_table["customers"].per_column)
        assert per_column["email"].deterministic is True

"""Shared fixtures for planner unit tests."""

from __future__ import annotations

from datetime import datetime

import pytest

from decoy_engine.profile import (
    ColumnProfile,
    Profile,
    Relationship,
    TableProfile,
)


def _col(
    name: str,
    *,
    dtype: str = "object",
    row_count: int = 10,
    null_count: int = 0,
    distinct_count: int | None = 10,
    sampled: bool = False,
    is_candidate_key_sampled: bool = False,
    declared_pk: bool = False,
    is_fk: bool = False,
    fk_target: tuple[str, str] | None = None,
) -> ColumnProfile:
    return ColumnProfile(
        name=name,
        dtype=dtype,
        row_count=row_count,
        null_count=null_count,
        distinct_count=distinct_count,
        sampled=sampled,
        is_candidate_key_sampled=is_candidate_key_sampled,
        declared_pk=declared_pk,
        is_fk=is_fk,
        fk_target=fk_target,
        pii_class=None,
    )


@pytest.fixture
def simple_profile() -> Profile:
    """A two-table profile with a single-column FK customers -> orders."""
    customers = TableProfile(
        name="customers",
        row_count=10,
        columns=(
            _col("customer_id", declared_pk=True, is_candidate_key_sampled=True),
            _col("name"),
        ),
    )
    orders = TableProfile(
        name="orders",
        row_count=20,
        columns=(
            _col(
                "order_id",
                row_count=20,
                declared_pk=True,
                is_candidate_key_sampled=True,
                distinct_count=20,
            ),
            _col(
                "customer_id",
                row_count=20,
                is_fk=True,
                fk_target=("customers", "customer_id"),
                distinct_count=10,
            ),
        ),
    )
    return Profile(
        schema_version=1,
        tables=(customers, orders),
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


@pytest.fixture
def composite_profile() -> Profile:
    """A two-table profile with a composite-key FK enrollments -> claims."""
    enrollments = TableProfile(
        name="enrollments",
        row_count=5,
        columns=(
            _col("member_id", declared_pk=True, row_count=5, distinct_count=5),
            _col("plan_id", declared_pk=True, row_count=5, distinct_count=3),
            _col("effective_date", declared_pk=True, row_count=5, distinct_count=5),
        ),
    )
    claims = TableProfile(
        name="claims",
        row_count=20,
        columns=(
            _col(
                "claim_id",
                declared_pk=True,
                row_count=20,
                distinct_count=20,
                is_candidate_key_sampled=True,
            ),
            _col(
                "member_id",
                row_count=20,
                is_fk=True,
                fk_target=("enrollments", "member_id"),
                distinct_count=5,
            ),
            _col(
                "plan_id",
                row_count=20,
                is_fk=True,
                fk_target=("enrollments", "plan_id"),
                distinct_count=3,
            ),
            _col(
                "effective_date",
                row_count=20,
                is_fk=True,
                fk_target=("enrollments", "effective_date"),
                distinct_count=5,
            ),
        ),
    )
    return Profile(
        schema_version=1,
        tables=(enrollments, claims),
        relationships=(
            Relationship(
                parent_table="enrollments",
                parent_columns=("member_id", "plan_id", "effective_date"),
                child_table="claims",
                child_columns=("member_id", "plan_id", "effective_date"),
                namespace="enrollment_identity",
            ),
        ),
        profiled_at=datetime(2026, 5, 27, 0, 0, 0),
        decoy_engine_version="0.1.0",
    )


@pytest.fixture
def simple_config() -> dict:
    """Minimal config that compiles cleanly against simple_profile."""
    return {
        "global_settings": {"seed": 42},
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
        "namespaces": {
            "customer_identity": {"declared_by": ["customers.customer_id", "orders.customer_id"]}
        },
    }

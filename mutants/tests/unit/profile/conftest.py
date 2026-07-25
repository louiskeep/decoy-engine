"""Shared fixtures for profile-module unit tests."""

from __future__ import annotations

from datetime import datetime

import pytest

from decoy_engine.profile import (
    ColumnProfile,
    PIIClass,
    Profile,
    Relationship,
    TableProfile,
)


@pytest.fixture
def sample_column() -> ColumnProfile:
    return ColumnProfile(
        name="customer_id",
        dtype="int64",
        row_count=1000,
        null_count=0,
        distinct_count=1000,
        sampled=False,
        is_candidate_key_sampled=True,
        declared_pk=True,
        is_fk=False,
        fk_target=None,
        pii_class=None,
    )


@pytest.fixture
def sample_pii_column() -> ColumnProfile:
    return ColumnProfile(
        name="email",
        dtype="object",
        row_count=1000,
        null_count=12,
        distinct_count=987,
        sampled=False,
        is_candidate_key_sampled=False,
        declared_pk=False,
        is_fk=False,
        fk_target=None,
        pii_class=PIIClass.EMAIL,
    )


@pytest.fixture
def sample_fk_column() -> ColumnProfile:
    return ColumnProfile(
        name="customer_id",
        dtype="int64",
        row_count=5000,
        null_count=0,
        distinct_count=1000,
        sampled=False,
        is_candidate_key_sampled=False,
        declared_pk=False,
        is_fk=True,
        fk_target=("customers", "customer_id"),
        pii_class=None,
    )


@pytest.fixture
def sample_table(sample_column: ColumnProfile, sample_pii_column: ColumnProfile) -> TableProfile:
    return TableProfile(
        name="customers",
        row_count=1000,
        columns=(sample_column, sample_pii_column),
    )


@pytest.fixture
def sample_relationship() -> Relationship:
    return Relationship(
        parent_table="customers",
        parent_columns=("customer_id",),
        child_table="orders",
        child_columns=("customer_id",),
        namespace="customer_identity",
    )


@pytest.fixture
def sample_composite_relationship() -> Relationship:
    return Relationship(
        parent_table="enrollments",
        parent_columns=("member_id", "plan_id", "effective_date"),
        child_table="claims",
        child_columns=("member_id", "plan_id", "effective_date"),
        namespace="enrollment_identity",
    )


@pytest.fixture
def sample_profile(
    sample_table: TableProfile,
    sample_relationship: Relationship,
) -> Profile:
    return Profile(
        schema_version=1,
        tables=(sample_table,),
        relationships=(sample_relationship,),
        profiled_at=datetime(2026, 5, 26, 12, 0, 0),
        decoy_engine_version="0.1.0",
        profile_seed=42,
    )

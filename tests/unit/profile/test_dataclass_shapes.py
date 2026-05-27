"""Shape and immutability tests for Profile dataclasses.

Covers: field presence, frozen-ness, value equality, composite-key
relationships at length>1, and the spec's PIIClass enum membership.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from datetime import datetime

import pytest

from decoy_engine.profile import (
    ColumnProfile,
    PIIClass,
    Profile,
    Relationship,
    TableProfile,
)


class TestColumnProfile:
    def test_has_expected_fields(self, sample_column: ColumnProfile) -> None:
        field_names = {f.name for f in fields(sample_column)}
        assert field_names == {
            "name",
            "dtype",
            "row_count",
            "null_count",
            "distinct_count",
            "sampled",
            "is_candidate_key_sampled",
            "declared_pk",
            "is_fk",
            "fk_target",
            "pii_class",
        }

    def test_is_frozen(self, sample_column: ColumnProfile) -> None:
        with pytest.raises(FrozenInstanceError):
            sample_column.name = "renamed"  # type: ignore[misc]

    def test_value_equality(self) -> None:
        a = ColumnProfile(
            name="x",
            dtype="int64",
            row_count=10,
            null_count=0,
            distinct_count=10,
            sampled=False,
            is_candidate_key_sampled=True,
            declared_pk=True,
            is_fk=False,
            fk_target=None,
            pii_class=None,
        )
        b = ColumnProfile(
            name="x",
            dtype="int64",
            row_count=10,
            null_count=0,
            distinct_count=10,
            sampled=False,
            is_candidate_key_sampled=True,
            declared_pk=True,
            is_fk=False,
            fk_target=None,
            pii_class=None,
        )
        assert a == b
        assert hash(a) == hash(b)


class TestRelationship:
    def test_single_column_is_length_one_tuple(self, sample_relationship: Relationship) -> None:
        assert sample_relationship.parent_columns == ("customer_id",)
        assert sample_relationship.child_columns == ("customer_id",)
        assert len(sample_relationship.parent_columns) == 1

    def test_composite_columns(self, sample_composite_relationship: Relationship) -> None:
        rel = sample_composite_relationship
        assert len(rel.parent_columns) == 3
        assert rel.parent_columns == ("member_id", "plan_id", "effective_date")
        assert rel.child_columns == ("member_id", "plan_id", "effective_date")

    def test_is_frozen(self, sample_relationship: Relationship) -> None:
        with pytest.raises(FrozenInstanceError):
            sample_relationship.namespace = "different"  # type: ignore[misc]


class TestTableProfile:
    def test_has_expected_fields(self, sample_table: TableProfile) -> None:
        field_names = {f.name for f in fields(sample_table)}
        assert field_names == {"name", "row_count", "columns"}

    def test_columns_is_tuple(self, sample_table: TableProfile) -> None:
        assert isinstance(sample_table.columns, tuple)


class TestProfile:
    def test_has_expected_fields(self, sample_profile: Profile) -> None:
        field_names = {f.name for f in fields(sample_profile)}
        assert field_names == {
            "schema_version",
            "tables",
            "relationships",
            "profiled_at",
            "decoy_engine_version",
            "profile_seed",
        }

    def test_is_frozen(self, sample_profile: Profile) -> None:
        with pytest.raises(FrozenInstanceError):
            sample_profile.schema_version = 99  # type: ignore[misc]

    def test_profile_seed_defaults_to_none(self, sample_table: TableProfile) -> None:
        profile = Profile(
            schema_version=1,
            tables=(sample_table,),
            relationships=(),
            profiled_at=datetime(2026, 5, 26, 12, 0, 0),
            decoy_engine_version="0.1.0",
        )
        assert profile.profile_seed is None


class TestPIIClass:
    def test_storm_built_in_detectors_are_represented(self) -> None:
        # Mirrors decoy_engine.storm.detectors.REGISTERED_DETECTORS exactly.
        # The cross-module symmetry test in test_pii_storm_sync.py is the
        # authoritative guard; this set is a local human-readable copy.
        # Update both lists together when STORM adds a new detector.
        expected_built_ins = {
            "email",
            "ssn",
            "us_phone",
            "us_zip",
            "first_name",
            "last_name",
            "person_name",
            "address",
            "iso_date",
            "us_date",
            "eu_date",
            "pan",
            "cvv",
            "iban",
            "ipv4",
            "icd10",
            "npi",
            "mrn",
            "url",
            "fax_number",
            "health_plan_id",
            "license_num",
            "vehicle_id",
            "device_id",
            "biometric_id",
        }
        actual = {member.value for member in PIIClass}
        assert actual == expected_built_ins

    def test_str_inheritance(self) -> None:
        # PIIClass values are strings, so they compare/serialize like strings.
        assert PIIClass.EMAIL == "email"
        assert isinstance(PIIClass.EMAIL, str)

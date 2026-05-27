"""JSON serialization round-trip for Profile.

Asserts profile_to_json + profile_from_json produces an instance equal
to the original, including datetime (ISO-8601), PIIClass enum, FK
target tuple, and composite Relationship column tuples.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from decoy_engine.profile import (
    ColumnProfile,
    PIIClass,
    Profile,
    Relationship,
    TableProfile,
    profile_from_json,
    profile_to_json,
)


class TestJsonRoundTrip:
    def test_simple_profile_round_trips(self, sample_profile: Profile) -> None:
        s = profile_to_json(sample_profile)
        recovered = profile_from_json(s)
        assert recovered == sample_profile

    def test_round_trip_preserves_datetime(self, sample_profile: Profile) -> None:
        s = profile_to_json(sample_profile)
        recovered = profile_from_json(s)
        assert recovered.profiled_at == sample_profile.profiled_at

    def test_round_trip_preserves_pii_class(
        self,
        sample_table: TableProfile,
        sample_pii_column: ColumnProfile,
    ) -> None:
        profile = Profile(
            schema_version=1,
            tables=(sample_table,),
            relationships=(),
            profiled_at=datetime(2026, 5, 26, 12, 0, 0),
            decoy_engine_version="0.1.0",
        )
        s = profile_to_json(profile)
        recovered = profile_from_json(s)
        assert recovered.tables[0].columns[1].pii_class == PIIClass.EMAIL

    def test_round_trip_preserves_fk_target_tuple(
        self,
        sample_fk_column: ColumnProfile,
    ) -> None:
        table = TableProfile(name="orders", row_count=5000, columns=(sample_fk_column,))
        profile = Profile(
            schema_version=1,
            tables=(table,),
            relationships=(),
            profiled_at=datetime(2026, 5, 26, 12, 0, 0),
            decoy_engine_version="0.1.0",
        )
        s = profile_to_json(profile)
        recovered = profile_from_json(s)
        assert recovered.tables[0].columns[0].fk_target == ("customers", "customer_id")
        assert isinstance(recovered.tables[0].columns[0].fk_target, tuple)

    def test_round_trip_preserves_composite_relationship(
        self,
        sample_composite_relationship: Relationship,
    ) -> None:
        profile = Profile(
            schema_version=1,
            tables=(),
            relationships=(sample_composite_relationship,),
            profiled_at=datetime(2026, 5, 26, 12, 0, 0),
            decoy_engine_version="0.1.0",
        )
        s = profile_to_json(profile)
        recovered = profile_from_json(s)
        rel = recovered.relationships[0]
        assert rel.parent_columns == ("member_id", "plan_id", "effective_date")
        assert rel.child_columns == ("member_id", "plan_id", "effective_date")
        assert isinstance(rel.parent_columns, tuple)

    def test_json_is_well_formed(self, sample_profile: Profile) -> None:
        s = profile_to_json(sample_profile)
        json.loads(s)


class TestJsonErrorPaths:
    def test_invalid_json_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="invalid JSON"):
            profile_from_json("{not json")

    def test_missing_required_field_raises(self) -> None:
        with pytest.raises((KeyError, ValueError)):
            profile_from_json('{"schema_version": 1}')

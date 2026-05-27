"""B1 invariant: profile_hash excludes sidecar metadata.

Two Profile instances with identical data-shape fields (schema_version,
tables, relationships) must produce equal profile_hash values even when
their profiled_at, decoy_engine_version, or profile_seed differ. This is
the contract that lets compile_plan(config, profile, version) produce
byte-identical Plans across repeat compiles.
"""

from __future__ import annotations

from datetime import datetime

from decoy_engine.profile import (
    ColumnProfile,
    Profile,
    Relationship,
    TableProfile,
    profile_hash,
)


def _build_profile(
    *,
    profiled_at: datetime,
    decoy_engine_version: str = "0.1.0",
    profile_seed: int | None = None,
) -> Profile:
    """Construct a fixed-shape profile parameterized only by sidecar metadata."""
    column = ColumnProfile(
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
    table = TableProfile(name="customers", row_count=1000, columns=(column,))
    relationship = Relationship(
        parent_table="customers",
        parent_columns=("customer_id",),
        child_table="orders",
        child_columns=("customer_id",),
        namespace="customer_identity",
    )
    return Profile(
        schema_version=1,
        tables=(table,),
        relationships=(relationship,),
        profiled_at=profiled_at,
        decoy_engine_version=decoy_engine_version,
        profile_seed=profile_seed,
    )


class TestProfileHashInvariance:
    def test_different_timestamps_equal_hash(self) -> None:
        p1 = _build_profile(profiled_at=datetime(2026, 1, 1, 0, 0, 0))
        p2 = _build_profile(profiled_at=datetime(2026, 12, 31, 23, 59, 59))
        assert p1 != p2
        assert profile_hash(p1) == profile_hash(p2)

    def test_different_engine_versions_equal_hash(self) -> None:
        p1 = _build_profile(
            profiled_at=datetime(2026, 5, 26, 12, 0, 0),
            decoy_engine_version="0.1.0",
        )
        p2 = _build_profile(
            profiled_at=datetime(2026, 5, 26, 12, 0, 0),
            decoy_engine_version="0.2.0",
        )
        assert p1 != p2
        assert profile_hash(p1) == profile_hash(p2)

    def test_different_profile_seeds_equal_hash(self) -> None:
        p1 = _build_profile(profiled_at=datetime(2026, 5, 26, 12, 0, 0), profile_seed=42)
        p2 = _build_profile(profiled_at=datetime(2026, 5, 26, 12, 0, 0), profile_seed=99)
        assert p1 != p2
        assert profile_hash(p1) == profile_hash(p2)


class TestProfileHashSensitivity:
    """The hash must change when any data-shape field changes."""

    def test_different_schema_version_changes_hash(self) -> None:
        p1 = _build_profile(profiled_at=datetime(2026, 5, 26, 12, 0, 0))
        p2_data = Profile(
            schema_version=2,
            tables=p1.tables,
            relationships=p1.relationships,
            profiled_at=p1.profiled_at,
            decoy_engine_version=p1.decoy_engine_version,
            profile_seed=p1.profile_seed,
        )
        assert profile_hash(p1) != profile_hash(p2_data)

    def test_different_row_count_changes_hash(self) -> None:
        p1 = _build_profile(profiled_at=datetime(2026, 5, 26, 12, 0, 0))
        modified_column = ColumnProfile(
            name="customer_id",
            dtype="int64",
            row_count=2000,
            null_count=0,
            distinct_count=2000,
            sampled=False,
            is_candidate_key_sampled=True,
            declared_pk=True,
            is_fk=False,
            fk_target=None,
            pii_class=None,
        )
        modified_table = TableProfile(name="customers", row_count=2000, columns=(modified_column,))
        p2 = Profile(
            schema_version=p1.schema_version,
            tables=(modified_table,),
            relationships=p1.relationships,
            profiled_at=p1.profiled_at,
            decoy_engine_version=p1.decoy_engine_version,
            profile_seed=p1.profile_seed,
        )
        assert profile_hash(p1) != profile_hash(p2)

    def test_different_relationship_changes_hash(self) -> None:
        p1 = _build_profile(profiled_at=datetime(2026, 5, 26, 12, 0, 0))
        different_rel = Relationship(
            parent_table="customers",
            parent_columns=("customer_id",),
            child_table="invoices",
            child_columns=("customer_id",),
            namespace="customer_identity",
        )
        p2 = Profile(
            schema_version=p1.schema_version,
            tables=p1.tables,
            relationships=(different_rel,),
            profiled_at=p1.profiled_at,
            decoy_engine_version=p1.decoy_engine_version,
            profile_seed=p1.profile_seed,
        )
        assert profile_hash(p1) != profile_hash(p2)


class TestProfileHashDeterminism:
    def test_hash_is_deterministic_for_equal_profiles(self) -> None:
        p1 = _build_profile(profiled_at=datetime(2026, 5, 26, 12, 0, 0))
        p2 = _build_profile(profiled_at=datetime(2026, 5, 26, 12, 0, 0))
        assert p1 == p2
        assert profile_hash(p1) == profile_hash(p2)

    def test_hash_format_is_64_hex_chars(self) -> None:
        p = _build_profile(profiled_at=datetime(2026, 5, 26, 12, 0, 0))
        h = profile_hash(p)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

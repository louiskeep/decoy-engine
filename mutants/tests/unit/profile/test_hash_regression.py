"""Pinned-hash regression test (M4 from slice-1 review).

A canonical Profile that spans every supported field type is hashed to
a literal hex string baked into this file. Any silent change to the
canonical-bytes format (field ordering in _data_shape_bytes, JSON
separators, encoding, enum value rendering, tuple-to-list coercion in
the column-dict shape, etc.) breaks the pin and trips this test.

When the canonical format intentionally changes (rare, deliberate, and
release-noted), regenerate the pinned hash by running:

    python -c "from tests.unit.profile.test_hash_regression import \
        build_regression_profile; \
        from decoy_engine.profile import profile_hash; \
        print(profile_hash(build_regression_profile()))"

then paste the new value into PINNED_HASH and add a release note
explaining the bump. Do NOT regenerate to silence a failing test
without first understanding what changed in the canonical format.
"""

from __future__ import annotations

from datetime import datetime

from decoy_engine.profile import (
    ColumnProfile,
    PIIClass,
    Profile,
    Relationship,
    TableProfile,
    profile_hash,
)

# Canonical regression profile: deliberately spans every supported field
# type so any change to the canonical-bytes format trips the pin.
# - Single-column FK relationship
# - Composite (length-3) FK relationship
# - PIIClass enum value
# - Sampled column (sampled=True, candidate_key=False)
# - Full-scan column (sampled=False, candidate_key=True)
# - fk_target tuple
# - null_count > 0 on one column
PINNED_HASH = "eb129188e301c4a2ddea96438c8aa42a40da99af4a9286362f3d01164dfc7ddf"


def build_regression_profile() -> Profile:
    parent_col = ColumnProfile(
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
    email_col = ColumnProfile(
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
    fk_col = ColumnProfile(
        name="customer_id",
        dtype="int64",
        row_count=5000,
        null_count=0,
        distinct_count=1000,
        sampled=True,
        is_candidate_key_sampled=False,
        declared_pk=False,
        is_fk=True,
        fk_target=("customers", "customer_id"),
        pii_class=None,
    )
    customers = TableProfile(
        name="customers",
        row_count=1000,
        columns=(parent_col, email_col),
    )
    orders = TableProfile(
        name="orders",
        row_count=5000,
        columns=(fk_col,),
    )
    single_rel = Relationship(
        parent_table="customers",
        parent_columns=("customer_id",),
        child_table="orders",
        child_columns=("customer_id",),
        namespace="customer_identity",
    )
    composite_rel = Relationship(
        parent_table="enrollments",
        parent_columns=("member_id", "plan_id", "effective_date"),
        child_table="claims",
        child_columns=("member_id", "plan_id", "effective_date"),
        namespace="enrollment_identity",
    )
    return Profile(
        schema_version=1,
        tables=(customers, orders),
        relationships=(single_rel, composite_rel),
        profiled_at=datetime(2026, 5, 26, 12, 0, 0),
        decoy_engine_version="0.1.0",
        profile_seed=42,
    )


def test_regression_hash_matches_pin() -> None:
    """If this fails, the canonical-bytes format changed.

    See the module docstring for the regeneration procedure. Do not
    silence a failure by regenerating without understanding the cause.
    """
    profile = build_regression_profile()
    assert profile_hash(profile) == PINNED_HASH


def test_pinned_hash_is_64_hex_chars() -> None:
    assert len(PINNED_HASH) == 64
    assert all(c in "0123456789abcdef" for c in PINNED_HASH)

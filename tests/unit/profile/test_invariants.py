"""Negative tests for Profile-layer invariants added in the slice-1 review.

These cover the M1-M3 and L1-L2 findings from the Dennis slice-1 review:

  M1: Relationship parent/child column length match + non-empty
  M2: TableProfile column-name uniqueness
  M3: Profile table-name uniqueness
  L1: ColumnProfile is_fk <-> fk_target consistency
  L2: ColumnProfile null_count / distinct_count <= row_count

Each invariant has at least one negative test (assertion that constructing the
invalid shape raises ValueError) and at least one boundary positive test
(equivalent valid shape constructs cleanly). The H6 candidate-key invariant
already has its own dedicated file at test_candidate_key.py.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from decoy_engine.profile import (
    ColumnProfile,
    Profile,
    Relationship,
    TableProfile,
)


def _valid_column(name: str = "id", *, declared_pk: bool = True) -> ColumnProfile:
    return ColumnProfile(
        name=name,
        dtype="int64",
        row_count=10,
        null_count=0,
        distinct_count=10,
        sampled=False,
        is_candidate_key_sampled=True,
        declared_pk=declared_pk,
        is_fk=False,
        fk_target=None,
        pii_class=None,
    )


class TestRelationshipInvariants:
    """M1: parent/child columns same length, both non-empty."""

    def test_length_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="parent_columns length 2 != child_columns length 1"):
            Relationship(
                parent_table="enrollments",
                parent_columns=("member_id", "plan_id"),
                child_table="claims",
                child_columns=("member_id",),
                namespace=None,
            )

    def test_empty_parent_columns_raises(self) -> None:
        with pytest.raises(ValueError, match="must both be non-empty"):
            Relationship(
                parent_table="p",
                parent_columns=(),
                child_table="c",
                child_columns=(),
                namespace=None,
            )

    def test_empty_child_columns_raises(self) -> None:
        with pytest.raises(ValueError, match="must both be non-empty"):
            Relationship(
                parent_table="p",
                parent_columns=("a",),
                child_table="c",
                child_columns=(),
                namespace=None,
            )

    def test_single_column_is_valid(self) -> None:
        rel = Relationship(
            parent_table="customers",
            parent_columns=("customer_id",),
            child_table="orders",
            child_columns=("customer_id",),
            namespace="customer_identity",
        )
        assert rel.parent_columns == ("customer_id",)

    def test_composite_same_length_is_valid(self) -> None:
        rel = Relationship(
            parent_table="enrollments",
            parent_columns=("member_id", "plan_id", "effective_date"),
            child_table="claims",
            child_columns=("member_id", "plan_id", "effective_date"),
            namespace="enrollment_identity",
        )
        assert len(rel.parent_columns) == 3


class TestTableProfileInvariants:
    """M2: column names unique within a table."""

    def test_duplicate_column_names_raises(self) -> None:
        c1 = _valid_column(name="id", declared_pk=True)
        c2 = _valid_column(name="id", declared_pk=False)
        with pytest.raises(ValueError, match="duplicate column names \\['id'\\]"):
            TableProfile(name="t", row_count=10, columns=(c1, c2))

    def test_distinct_column_names_are_valid(self) -> None:
        c1 = _valid_column(name="customer_id")
        c2 = _valid_column(name="email", declared_pk=False)
        tbl = TableProfile(name="customers", row_count=10, columns=(c1, c2))
        assert {c.name for c in tbl.columns} == {"customer_id", "email"}

    def test_error_message_lists_all_duplicates(self) -> None:
        c1 = _valid_column(name="id", declared_pk=True)
        c2 = _valid_column(name="id", declared_pk=False)
        c3 = _valid_column(name="x", declared_pk=False)
        c4 = _valid_column(name="x", declared_pk=False)
        with pytest.raises(ValueError, match="\\['id', 'x'\\]"):
            TableProfile(name="t", row_count=10, columns=(c1, c2, c3, c4))


class TestProfileInvariants:
    """M3: table names unique within a Profile."""

    def test_duplicate_table_names_raises(self) -> None:
        col = _valid_column(name="id")
        t1 = TableProfile(name="customers", row_count=10, columns=(col,))
        t2 = TableProfile(name="customers", row_count=20, columns=(col,))
        with pytest.raises(ValueError, match="duplicate table names \\['customers'\\]"):
            Profile(
                schema_version=1,
                tables=(t1, t2),
                relationships=(),
                profiled_at=datetime(2026, 5, 26, 12, 0, 0),
                decoy_engine_version="0.1.0",
            )

    def test_distinct_table_names_are_valid(self) -> None:
        col = _valid_column(name="id")
        t1 = TableProfile(name="customers", row_count=10, columns=(col,))
        t2 = TableProfile(name="orders", row_count=20, columns=(col,))
        profile = Profile(
            schema_version=1,
            tables=(t1, t2),
            relationships=(),
            profiled_at=datetime(2026, 5, 26, 12, 0, 0),
            decoy_engine_version="0.1.0",
        )
        assert {t.name for t in profile.tables} == {"customers", "orders"}


class TestColumnProfileFkConsistency:
    """L1: is_fk <-> (fk_target is not None) must agree."""

    def test_is_fk_true_with_none_target_raises(self) -> None:
        with pytest.raises(ValueError, match="is_fk=True but fk_target=None"):
            ColumnProfile(
                name="customer_id",
                dtype="int64",
                row_count=10,
                null_count=0,
                distinct_count=10,
                sampled=False,
                is_candidate_key_sampled=False,
                declared_pk=False,
                is_fk=True,
                fk_target=None,
                pii_class=None,
            )

    def test_is_fk_false_with_target_raises(self) -> None:
        with pytest.raises(ValueError, match="is_fk=False but fk_target="):
            ColumnProfile(
                name="customer_id",
                dtype="int64",
                row_count=10,
                null_count=0,
                distinct_count=10,
                sampled=False,
                is_candidate_key_sampled=False,
                declared_pk=False,
                is_fk=False,
                fk_target=("customers", "customer_id"),
                pii_class=None,
            )

    def test_both_set_is_valid(self) -> None:
        col = ColumnProfile(
            name="customer_id",
            dtype="int64",
            row_count=10,
            null_count=0,
            distinct_count=10,
            sampled=False,
            is_candidate_key_sampled=False,
            declared_pk=False,
            is_fk=True,
            fk_target=("customers", "customer_id"),
            pii_class=None,
        )
        assert col.is_fk is True
        assert col.fk_target == ("customers", "customer_id")

    def test_both_unset_is_valid(self) -> None:
        col = ColumnProfile(
            name="email",
            dtype="object",
            row_count=10,
            null_count=0,
            distinct_count=10,
            sampled=False,
            is_candidate_key_sampled=False,
            declared_pk=False,
            is_fk=False,
            fk_target=None,
            pii_class=None,
        )
        assert col.is_fk is False
        assert col.fk_target is None


class TestColumnProfileCardinalitySanity:
    """L2: null_count and distinct_count must not exceed row_count."""

    def test_null_count_exceeds_row_count_raises(self) -> None:
        with pytest.raises(ValueError, match="null_count=11 exceeds row_count=10"):
            ColumnProfile(
                name="x",
                dtype="object",
                row_count=10,
                null_count=11,
                distinct_count=0,
                sampled=False,
                is_candidate_key_sampled=False,
                declared_pk=False,
                is_fk=False,
                fk_target=None,
                pii_class=None,
            )

    def test_distinct_count_exceeds_row_count_raises(self) -> None:
        with pytest.raises(ValueError, match="distinct_count=11 exceeds row_count=10"):
            ColumnProfile(
                name="x",
                dtype="int64",
                row_count=10,
                null_count=0,
                distinct_count=11,
                sampled=False,
                is_candidate_key_sampled=False,
                declared_pk=False,
                is_fk=False,
                fk_target=None,
                pii_class=None,
            )

    def test_distinct_count_none_is_allowed(self) -> None:
        col = ColumnProfile(
            name="x",
            dtype="object",
            row_count=10,
            null_count=0,
            distinct_count=None,
            sampled=True,
            is_candidate_key_sampled=False,
            declared_pk=False,
            is_fk=False,
            fk_target=None,
            pii_class=None,
        )
        assert col.distinct_count is None

    def test_boundary_counts_are_valid(self) -> None:
        col = ColumnProfile(
            name="x",
            dtype="int64",
            row_count=10,
            null_count=10,
            distinct_count=10,
            sampled=False,
            is_candidate_key_sampled=False,
            declared_pk=False,
            is_fk=False,
            fk_target=None,
            pii_class=None,
        )
        assert col.null_count == col.row_count
        assert col.distinct_count == col.row_count

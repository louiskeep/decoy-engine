"""Tests for `compare()` — schema-drift comparator.

Drift is structural only. Row-counts intentionally out of scope; the
test suite codifies that contract so a future "row-count toggle"
addition is a deliberate scope expansion, not an accidental shift.
"""
from __future__ import annotations

from decoy_engine.walks import Column, Edge, SchemaSnapshot, Table, compare


def _col(name: str, *, nullable: bool = True, pk: bool = False, dtype: str = "integer") -> Column:
    return Column(name=name, data_type=dtype, nullable=nullable, is_primary_key=pk)


def _snap(tables: list[Table], edges: tuple[Edge, ...] = ()) -> SchemaSnapshot:
    return SchemaSnapshot(
        db_kind="postgres",
        schema_name="public",
        tables=tuple(tables),
        declared_edges=edges,
    )


def test_added_table_appears_in_added_only():
    a = _snap([Table("users", "public", (_col("id", pk=True, nullable=False),))])
    b = _snap([
        Table("users", "public", (_col("id", pk=True, nullable=False),)),
        Table("orders", "public", (_col("id", pk=True, nullable=False),)),
    ])
    drift = compare(a, b)
    assert drift.added_tables == ("orders",)
    assert drift.removed_tables == ()
    assert drift.changed_columns == ()


def test_removed_table_appears_in_removed_only():
    a = _snap([
        Table("users", "public", (_col("id", pk=True, nullable=False),)),
        Table("legacy_logs", "public", (_col("id", pk=True, nullable=False),)),
    ])
    b = _snap([
        Table("users", "public", (_col("id", pk=True, nullable=False),)),
    ])
    drift = compare(a, b)
    assert drift.removed_tables == ("legacy_logs",)
    assert drift.added_tables == ()


def test_added_column_to_existing_table():
    a = _snap([Table("users", "public", (_col("id", pk=True, nullable=False),))])
    b = _snap([Table("users", "public", (
        _col("id", pk=True, nullable=False),
        _col("email", dtype="varchar"),
    ))])
    drift = compare(a, b)
    assert len(drift.changed_columns) == 1
    assert drift.changed_columns[0] == {
        "table": "users",
        "column": "email",
        "change_kind": "added",
    }


def test_removed_column_from_existing_table():
    a = _snap([Table("users", "public", (
        _col("id", pk=True, nullable=False),
        _col("email", dtype="varchar"),
    ))])
    b = _snap([Table("users", "public", (
        _col("id", pk=True, nullable=False),
    ))])
    drift = compare(a, b)
    assert len(drift.changed_columns) == 1
    assert drift.changed_columns[0]["change_kind"] == "removed"
    assert drift.changed_columns[0]["column"] == "email"


def test_type_change_recorded_with_from_and_to():
    a = _snap([Table("users", "public", (
        _col("id", pk=True, nullable=False),
        _col("age", dtype="integer"),
    ))])
    b = _snap([Table("users", "public", (
        _col("id", pk=True, nullable=False),
        _col("age", dtype="bigint"),
    ))])
    drift = compare(a, b)
    type_changes = [c for c in drift.changed_columns if c["change_kind"] == "type_changed"]
    assert len(type_changes) == 1
    assert type_changes[0]["from"] == "integer"
    assert type_changes[0]["to"] == "bigint"


def test_nullability_change_recorded():
    a = _snap([Table("users", "public", (
        _col("id", pk=True, nullable=False),
        _col("email", dtype="varchar", nullable=True),
    ))])
    b = _snap([Table("users", "public", (
        _col("id", pk=True, nullable=False),
        _col("email", dtype="varchar", nullable=False),
    ))])
    drift = compare(a, b)
    null_changes = [c for c in drift.changed_columns if c["change_kind"] == "nullability_changed"]
    assert len(null_changes) == 1
    assert null_changes[0]["from"] is True
    assert null_changes[0]["to"] is False


def test_pk_change_recorded():
    """Demoting a PK is a structural change worth flagging — could
    indicate a schema migration in progress."""
    a = _snap([Table("users", "public", (
        _col("id", pk=True, nullable=False),
    ))])
    b = _snap([Table("users", "public", (
        _col("id", pk=False, nullable=False),
    ))])
    drift = compare(a, b)
    pk_changes = [c for c in drift.changed_columns if c["change_kind"] == "pk_changed"]
    assert len(pk_changes) == 1


def test_multiple_changes_on_same_column_emit_separate_records():
    """A column whose type AND nullability both shifted produces two
    entries — easier to act on than one merged record."""
    a = _snap([Table("users", "public", (
        _col("id", pk=True, nullable=False),
        _col("age", dtype="integer", nullable=True),
    ))])
    b = _snap([Table("users", "public", (
        _col("id", pk=True, nullable=False),
        _col("age", dtype="bigint", nullable=False),
    ))])
    drift = compare(a, b)
    age_changes = [c for c in drift.changed_columns if c["column"] == "age"]
    kinds = sorted(c["change_kind"] for c in age_changes)
    assert kinds == ["nullability_changed", "type_changed"]


def test_identical_snapshots_produce_empty_drift():
    snap = _snap([Table("users", "public", (
        _col("id", pk=True, nullable=False),
        _col("email", dtype="varchar"),
    ))])
    drift = compare(snap, snap)
    assert drift.added_tables == ()
    assert drift.removed_tables == ()
    assert drift.changed_columns == ()


def test_output_ordering_is_deterministic():
    """Sort by table-then-column ensures repeated runs produce identical
    diff output. Tests would be flaky otherwise."""
    a = _snap([
        Table("zzz", "public", (_col("id", pk=True, nullable=False),)),
        Table("aaa", "public", (_col("id", pk=True, nullable=False),)),
    ])
    b = _snap([
        Table("zzz", "public", (
            _col("id", pk=True, nullable=False),
            _col("z_col", dtype="varchar"),
        )),
        Table("aaa", "public", (
            _col("id", pk=True, nullable=False),
            _col("a_col", dtype="varchar"),
        )),
    ])
    drift = compare(a, b)
    # Tables iterated in sorted order, so aaa.a_col comes before zzz.z_col.
    assert [c["table"] for c in drift.changed_columns] == ["aaa", "zzz"]

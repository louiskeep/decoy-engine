"""Independent positive/negative controls for the strict parity comparator.

Each dimension is proven to bite on its own: an equal pair passes, and one
targeted difference fails that dimension. These are direct helper tests, not
tests that break a real route. Expected reports are hardcoded literals.
"""

from __future__ import annotations

import pyarrow as pa

from tests.parity import _strict_compare as sc

_ALL_TRUE = dict.fromkeys(sc.DIMENSIONS, True)


def _base() -> pa.Table:
    return pa.table(
        {
            "k": pa.array([0, 1, 2], type=pa.int64()),
            "f": pa.array([0.5, 1.5, 2.5], type=pa.float64()),
            "s": pa.array(["a", "b", "c"], type=pa.string()),
        }
    )


def _report(a: pa.Table, b: pa.Table) -> dict[str, bool]:
    return sc.compare_strict(a, b)


def test_identical_tables_agree_on_every_dimension() -> None:
    assert _report(_base(), _base()) == _ALL_TRUE


def test_dimension_names_are_pinned() -> None:
    assert sc.DIMENSIONS == (
        "schema_structure",
        "field_metadata",
        "type_width",
        "column_order",
        "row_count",
        "row_set",
        "row_order",
        "cellwise_values",
        "null_vs_nan",
    )


def test_schema_structure_negative_nullability() -> None:
    other = _base().cast(
        pa.schema(
            [
                pa.field("k", pa.int64(), nullable=False),
                pa.field("f", pa.float64()),
                pa.field("s", pa.string()),
            ]
        )
    )
    assert sc.schema_structure_equal(_base(), _base()) is True
    assert sc.schema_structure_equal(_base(), other) is False
    # Nullability alone: width, order, metadata and values are untouched.
    assert _report(_base(), other) == {**_ALL_TRUE, "schema_structure": False}


def test_field_metadata_negative_schema_level() -> None:
    other = _base().replace_schema_metadata({b"pandas": b"{}"})
    assert sc.field_metadata_equal(_base(), _base()) is True
    assert sc.field_metadata_equal(_base(), other) is False
    # The ONLY thing that differs is metadata; Table.equals ignores it by default.
    assert _base().equals(other) is True
    assert _report(_base(), other) == {**_ALL_TRUE, "field_metadata": False}


def test_field_metadata_negative_per_field() -> None:
    schema = _base().schema.set(0, _base().schema.field(0).with_metadata({b"unit": b"cm"}))
    other = _base().cast(schema)
    assert sc.field_metadata_equal(_base(), other) is False
    assert sc.schema_structure_equal(_base(), other) is True


def test_type_width_negative_int64_vs_float64() -> None:
    wide = _base().set_column(0, "k", pa.array([0.0, 1.0, 2.0], type=pa.float64()))
    assert sc.type_width_equal(_base(), _base()) is True
    assert sc.type_width_equal(_base(), wide) is False
    # Python `0 == 0.0`: the normalized harness would call these equal.
    assert [0, 1, 2] == [0.0, 1.0, 2.0]
    assert sc.cellwise_values_equal(_base(), wide) is False


def test_type_width_negative_string_vs_large_string() -> None:
    large = _base().set_column(2, "s", pa.array(["a", "b", "c"], type=pa.large_string()))
    assert sc.type_width_equal(_base(), large) is False
    assert sc.cellwise_values_equal(_base(), large) is True


def test_column_order_negative() -> None:
    swapped = _base().select(["f", "k", "s"])
    assert sc.column_order_equal(_base(), _base()) is True
    assert sc.column_order_equal(_base(), swapped) is False
    # Same columns, same types, same cells by name: only the order differs.
    assert _report(_base(), swapped) == {
        **_ALL_TRUE,
        "schema_structure": False,
        "column_order": False,
        "row_order": True,
    }


def test_row_count_negative() -> None:
    short = _base().slice(0, 2)
    assert sc.row_count_equal(_base(), _base()) is True
    assert sc.row_count_equal(_base(), short) is False


def test_row_order_negative_pure_reorder() -> None:
    reordered = _base().take([2, 1, 0])
    assert sc.row_order_equal(_base(), _base()) is True
    assert sc.row_order_equal(_base(), reordered) is False
    # Same row multiset, so the set check still holds.
    assert sc.row_set_equal(_base(), reordered) is True
    assert _report(_base(), reordered) == {
        **_ALL_TRUE,
        "row_order": False,
        "cellwise_values": False,
    }


def test_cellwise_value_negative_changed_cell() -> None:
    changed = _base().set_column(2, "s", pa.array(["a", "b", "X"], type=pa.string()))
    assert sc.cellwise_values_equal(_base(), _base()) is True
    assert sc.cellwise_values_equal(_base(), changed) is False
    assert sc.row_set_equal(_base(), changed) is False
    # Types, order and null/NaN positions are all unchanged.
    assert sc.type_width_equal(_base(), changed) is True
    assert sc.null_vs_nan_equal(_base(), changed) is True


def _float_tbl(values: list[float | None]) -> pa.Table:
    return pa.table({"f": pa.array(values, type=pa.float64())})


def test_same_position_nan_positive_control() -> None:
    nan = float("nan")
    a, b = _float_tbl([nan, 1.0]), _float_tbl([nan, 1.0])
    # Unchanged NaN-bearing output must NOT read as drift.
    assert sc.cellwise_values_equal(a, b) is True
    assert sc.null_vs_nan_equal(a, b) is True
    assert _report(a, b) == _ALL_TRUE


def test_null_vs_nan_negative() -> None:
    nan_tbl = _float_tbl([float("nan"), 1.0])
    null_tbl = _float_tbl([None, 1.0])
    assert sc.null_vs_nan_equal(nan_tbl, nan_tbl) is True
    assert sc.null_vs_nan_equal(nan_tbl, null_tbl) is False
    assert sc.cellwise_values_equal(nan_tbl, null_tbl) is False
    # Same schema, count and order: only the null-vs-NaN dimension (and the
    # cells that carry it) differ.
    assert _report(nan_tbl, null_tbl) == {
        **_ALL_TRUE,
        "row_set": False,
        "row_order": False,
        "cellwise_values": False,
        "null_vs_nan": False,
    }


def test_nan_at_different_position_is_not_equal() -> None:
    a = _float_tbl([float("nan"), 1.0])
    b = _float_tbl([1.0, float("nan")])
    assert sc.null_vs_nan_equal(a, b) is False
    assert sc.cellwise_values_equal(a, b) is False


def test_pinning_helpers() -> None:
    t = _base().replace_schema_metadata({b"pandas": b"{}", b"a": b"1"})
    assert sc.metadata_keys(t) == [b"a", b"pandas"]
    assert sc.metadata_keys(_base()) == []
    assert sc.schema_signature(_base()) == "k:int64,f:double,s:string"

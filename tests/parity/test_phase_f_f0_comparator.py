"""Phase F F0: the canonical comparator is itself tested.

An oracle is only as strong as its comparator, so every dimension the contract promises to
distinguish is shown to be distinguished (value, row order, column order, null vs NaN, Arrow
type, Python value type, nullability, field metadata, schema metadata, row count), and the one
dimension it promises to IGNORE (chunk layout) is shown to be ignored.
"""

from __future__ import annotations

import math
from typing import Any

import pyarrow as pa
import pytest

from tests.parity._f0_canonical import (
    canonical_diff,
    fingerprints_diff,
    rechunked,
    table_fingerprint,
)


def _base() -> pa.Table:
    return pa.table(
        {
            "i": pa.array([1, 2, None, 4], pa.int64()),
            "f": pa.array([1.5, math.nan, 3.0, None], pa.float64()),
            "s": pa.array(["a", "b", None, "d"], pa.string()),
        }
    )


def _differs(a: pa.Table, b: pa.Table) -> bool:
    by_diff = bool(canonical_diff(a, b))
    by_fp = table_fingerprint(a) != table_fingerprint(b)
    assert by_diff == by_fp, "readable diff and digest disagree"
    return by_diff


def test_identical_tables_are_equal() -> None:
    assert not _differs(_base(), _base())


def test_changed_value_is_detected() -> None:
    other = _base().set_column(0, "i", pa.array([1, 2, None, 5], pa.int64()))
    assert _differs(_base(), other)


def test_row_order_is_detected() -> None:
    reordered = _base().take([3, 2, 1, 0])
    assert _differs(_base(), reordered)


def test_column_order_is_detected() -> None:
    assert _differs(_base(), _base().select(["s", "f", "i"]))


def test_null_versus_nan_is_distinguished() -> None:
    nan_version = pa.table({"f": pa.array([1.0, math.nan], pa.float64())})
    null_version = pa.table({"f": pa.array([1.0, None], pa.float64())})
    assert _differs(nan_version, null_version)


def test_signed_zero_and_float_bits_are_distinguished() -> None:
    assert _differs(
        pa.table({"f": pa.array([0.0])}),
        pa.table({"f": pa.array([-0.0])}),
    )


def test_arrow_type_is_detected() -> None:
    narrower = _base().set_column(0, "i", pa.array([1, 2, None, 4], pa.int32()))
    assert _differs(_base(), narrower)


def test_int_versus_float_with_equal_numeric_value_is_detected() -> None:
    assert _differs(
        pa.table({"x": pa.array([1, 2], pa.int64())}),
        pa.table({"x": pa.array([1.0, 2.0], pa.float64())}),
    )


def test_nullability_is_detected() -> None:
    required = pa.schema([pa.field("x", pa.int64(), nullable=False)])
    optional = pa.schema([pa.field("x", pa.int64(), nullable=True)])
    a = pa.table({"x": pa.array([1, 2], pa.int64())}, schema=required)
    b = pa.table({"x": pa.array([1, 2], pa.int64())}, schema=optional)
    assert _differs(a, b)


def test_field_metadata_is_detected() -> None:
    tagged = pa.schema([pa.field("x", pa.int64(), metadata={"unit": "ms"})])
    plain = pa.schema([pa.field("x", pa.int64())])
    a = pa.table({"x": pa.array([1], pa.int64())}, schema=tagged)
    b = pa.table({"x": pa.array([1], pa.int64())}, schema=plain)
    assert _differs(a, b)


def test_schema_metadata_is_detected() -> None:
    a = _base().replace_schema_metadata({"k": "v"})
    assert _differs(a, _base())


def test_row_count_is_detected() -> None:
    assert _differs(_base(), _base().slice(0, 3))


@pytest.mark.parametrize("sizes", [[4], [1, 3], [3, 1], [1, 1, 1, 1], [0, 2, 0, 2, 0], [2, 2]])
def test_chunk_layout_is_not_part_of_equality(sizes: list[int]) -> None:
    laid_out = rechunked(_base(), sizes)
    assert not _differs(_base(), laid_out)
    assert fingerprints_diff(table_fingerprint(_base()), table_fingerprint(laid_out)) == []


def test_chunked_nested_and_binary_values_canonicalize() -> None:
    tbl = pa.table(
        {
            "l": pa.array([[1, 2], None, []], pa.list_(pa.int64())),
            "b": pa.array([b"\x00\x01", b"", None], pa.binary()),
        }
    )
    assert not _differs(tbl, rechunked(tbl, [1, 2]))
    changed: Any = tbl.set_column(0, "l", pa.array([[1, 3], None, []], pa.list_(pa.int64())))
    assert _differs(tbl, changed)


def test_fingerprints_diff_names_the_moved_column() -> None:
    other = _base().set_column(2, "s", pa.array(["a", "b", None, "z"], pa.string()))
    diffs = fingerprints_diff(table_fingerprint(_base()), table_fingerprint(other))
    assert any("column 's' values differ" in d for d in diffs)
    assert not any("column 'i'" in d for d in diffs)

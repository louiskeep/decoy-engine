"""Pandas nullable-dtype pricing on the prepared path, the masked cost labels,
and `pandas_nullable_columns` on malformed metadata (dennis round 2)."""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pyarrow as pa
import pytest

from decoy_engine.config._transforms import FilterOp
from decoy_engine.execution import _mem_estimate_arrow as _arrow
from decoy_engine.execution._fk_keys import to_pandas_fk_safe
from decoy_engine.execution._mem_estimate import _FIXED_WIDTH_DTYPE_BYTES, _column_bytes
from decoy_engine.execution._mem_estimate_arrow import classify_column, pandas_nullable_columns
from decoy_engine.execution._mem_estimate_schema import (
    table_size_spec_from_profile,
    table_size_spec_from_table,
)
from decoy_engine.execution._transforms_table import _apply_ops
from decoy_engine.profile._types import ColumnProfile, TableProfile

pd = pytest.importorskip("pandas")

_N = 1000


def _labels(spec: Any) -> dict[str, str]:
    return {c.name: c.dtype for c in spec.columns}


def _assert_never_below_actual(prepared: pa.Table) -> None:
    """The estimate must not undercut the pandas frame a full_frame run holds."""
    spec = table_size_spec_from_table("t", prepared)
    frame = to_pandas_fk_safe(prepared, [])
    for col in spec.columns:
        actual = frame[col.name].memory_usage(index=False, deep=True)
        assert _column_bytes(spec.row_count, col) >= actual, col.name


# --- prepared path -----------------------------------------------------------


def _scenario_a_source() -> pa.Table:
    vals = np.arange(_N)
    null_rows = vals % 10 == 0
    return pa.table(
        {
            "k": pa.array(vals),
            "a": pa.array(np.where(null_rows, 0, vals).astype("int32"), mask=null_rows),
            "b": pa.array(vals.astype("int64"), mask=null_rows),
            "f": pa.array(vals.astype("float32"), mask=null_rows),
        }
    )


def test_prepared_filter_removing_nulls_still_prices_the_widened_class() -> None:
    prepared = _apply_ops(_scenario_a_source(), [FilterOp(op="filter", expression="a > 0")])
    assert all(prepared.column(n).null_count == 0 for n in prepared.column_names)
    spec = table_size_spec_from_table("t", prepared)
    labels = _labels(spec)
    assert labels["a"] == "float64"
    assert labels["b"] == "Int64"
    _assert_never_below_actual(prepared)


def test_prepared_int64_with_nulls_is_priced_as_int64_extension() -> None:
    table = pa.table(
        {"b": pa.array([1, None, 3], pa.int64()), "u": pa.array([1, None, 3], pa.uint64())}
    )
    assert _labels(table_size_spec_from_table("t", table)) == {"b": "Int64", "u": "UInt64"}


def test_prepared_pandas_written_no_null_nullable_columns_are_widened() -> None:
    vals = np.arange(_N)
    frame = pd.DataFrame(
        {
            "k": vals,
            "a": pd.Series(vals % 100, dtype="Int8"),
            "bo": pd.Series(vals % 2 == 0, dtype="boolean"),
            "b": pd.Series(vals, dtype="Int64"),
            "f": pd.Series(vals.astype("float64"), dtype="Float64"),
        }
    )
    source = pa.Table.from_pandas(frame, preserve_index=False)
    prepared = _apply_ops(source, [FilterOp(op="filter", expression="k >= 0")])
    labels = _labels(table_size_spec_from_table("t", prepared))
    assert labels == {
        "k": "int64",
        "a": "float64",
        "bo": "pyobject[bool]",
        "b": "Int64",
        "f": "Float64",
    }
    _assert_never_below_actual(prepared)


# --- masked labels -----------------------------------------------------------


@pytest.mark.parametrize(
    ("arrow_type", "label"),
    [
        (pa.int64(), "Int64"),
        (pa.uint64(), "UInt64"),
        (pa.float64(), "Float64"),
        (pa.float32(), "Float32"),
    ],
)
def test_masked_classification_uses_the_nullable_label(arrow_type: pa.DataType, label: str) -> None:
    assert classify_column(arrow_type, has_nulls=True, masked=True).label == label  # type: ignore[union-attr]


@pytest.mark.parametrize("arrow_type", [pa.int64(), pa.uint64(), pa.float64(), pa.float32()])
def test_masked_label_costs_one_byte_more_than_the_plain_one(arrow_type: pa.DataType) -> None:
    plain = classify_column(arrow_type, has_nulls=False).label  # type: ignore[union-attr]
    masked = classify_column(arrow_type, has_nulls=True, masked=True).label  # type: ignore[union-attr]
    assert _FIXED_WIDTH_DTYPE_BYTES[masked] == _FIXED_WIDTH_DTYPE_BYTES[plain] + 1


def test_float16_has_no_nullable_extension_label() -> None:
    assert classify_column(pa.float16(), has_nulls=True, masked=True).label == "float16"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("label", "width"),
    [
        ("boolean", 2),
        ("Int8", 2),
        ("UInt8", 2),
        ("Int16", 3),
        ("UInt16", 3),
        ("Int32", 5),
        ("UInt32", 5),
        ("Float32", 5),
        ("Int64", 9),
        ("UInt64", 9),
        ("Float64", 9),
    ],
)
def test_nullable_extension_labels_carry_the_validity_byte(label: str, width: int) -> None:
    assert _FIXED_WIDTH_DTYPE_BYTES[label] == width


def _profile_table(names: list[str], rows: int) -> TableProfile:
    cols = tuple(
        ColumnProfile(
            name=n,
            dtype="int64",
            row_count=rows,
            null_count=0,
            distinct_count=rows,
            sampled=False,
            is_candidate_key_sampled=False,
            declared_pk=False,
            is_fk=False,
            fk_target=None,
            pii_class=None,
        )
        for n in names
    )
    return TableProfile(name="t", row_count=rows, columns=cols)


def test_profile_path_prices_no_null_pandas_64bit_and_float_columns_masked() -> None:
    frame = pd.DataFrame(
        {
            "i64": pd.array([1, 2, 3], "Int64"),
            "u64": pd.array([1, 2, 3], "UInt64"),
            "f64": pd.array([1.0, 2.0, 3.0], "Float64"),
            "f32": pd.array([1.0, 2.0, 3.0], "Float32"),
            "plain": pd.Series([1, 2, 3], dtype="int64"),
        }
    )
    table = pa.Table.from_pandas(frame, preserve_index=False)
    profile_table = _profile_table(list(frame.columns), 3)
    spec = table_size_spec_from_profile(
        profile_table,
        arrow_types=_arrow.column_arrow_types(table, profile_table),
        masked_columns=_arrow.source_nullable_columns(table),
    )
    assert _labels(spec) == {
        "i64": "Int64",
        "u64": "UInt64",
        "f64": "Float64",
        "f32": "Float32",
        "plain": "int64",
    }


# --- pandas_nullable_columns on malformed metadata ---------------------------


def _schema_with(raw: bytes | None) -> pa.Schema:
    meta = None if raw is None else {b"pandas": raw}
    return pa.schema([pa.field("a", pa.int8())], metadata=meta)


def _meta(columns: Any) -> bytes:
    return json.dumps({"columns": columns}).encode()


@pytest.mark.parametrize(
    "raw",
    [
        b"not json at all",
        b"\xff\xfe\x00 invalid utf-8",
        json.dumps([{"name": "a", "numpy_type": "Int8"}]).encode(),
        _meta({"name": "a", "numpy_type": "Int8"}),
        _meta(["a", 3, None]),
        _meta([{"name": "a", "numpy_type": ["Int8"]}]),
        _meta([{"numpy_type": "Int8"}]),
        b"{}",
        b"null",
        _meta(None),
    ],
    ids=[
        "non-json",
        "invalid-utf8",
        "top-level-list",
        "columns-is-dict",
        "non-dict-entries",
        "unhashable-numpy-type",
        "no-name",
        "empty-object",
        "json-null",
        "columns-null",
    ],
)
def test_malformed_pandas_metadata_marks_nothing(raw: bytes) -> None:
    assert pandas_nullable_columns(_schema_with(raw)) == frozenset()


def test_deeply_nested_pandas_metadata_marks_nothing() -> None:
    depth = 5000
    raw = b"[" * depth + b"]" * depth
    assert pandas_nullable_columns(_schema_with(raw)) == frozenset()


def test_no_pandas_metadata_marks_nothing() -> None:
    assert pandas_nullable_columns(_schema_with(None)) == frozenset()


def test_field_name_wins_over_name_and_name_is_the_fallback() -> None:
    raw = _meta(
        [
            {"name": "display", "field_name": "stored", "numpy_type": "Int8"},
            {"name": "fallback", "numpy_type": "boolean"},
            {"name": "plain", "field_name": "plain", "numpy_type": "int8"},
        ]
    )
    assert pandas_nullable_columns(_schema_with(raw)) == frozenset({"stored", "fallback"})

"""Inputs: refusals, accepted types, value-free errors, no mutation, pandas equals Arrow."""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.anonymize.microaggregation import (
    MAX_ROWS,
    MicroaggregationResult,
    _api,
    microaggregate,
)

SECRET = 7731.25
BASE = [SECRET, 1.0, 2.0, 3.0, 10.0, 11.0]


def _frame(**columns: Any) -> pd.DataFrame:
    return pd.DataFrame({"a": BASE, **columns})


def _assert_clean(exc: BaseException) -> None:
    assert "7731" not in str(exc)
    assert exc.__cause__ is None
    assert exc.__context__ is None


# ------------------------------------------------------------------ refusals

REFUSALS: list[tuple[str, Any, Any, dict[str, Any], type[Exception], str]] = [
    ("table type", BASE, ["a"], {"k": 2}, TypeError, "pandas DataFrame or a pyarrow Table"),
    ("k bool", None, ["a"], {"k": True}, TypeError, "k must be an int"),
    ("k float", None, ["a"], {"k": 3.0}, TypeError, "k must be an int"),
    ("k str", None, ["a"], {"k": "3"}, TypeError, "k must be an int"),
    ("k numpy", None, ["a"], {"k": np.int64(3)}, TypeError, "k must be an int"),
    ("k one", None, ["a"], {"k": 1}, ValueError, "k must be at least 2"),
    ("k zero", None, ["a"], {"k": 0}, ValueError, "k must be at least 2"),
    ("k negative", None, ["a"], {"k": -2}, ValueError, "k must be at least 2"),
    ("qi str", None, "a", {"k": 2}, TypeError, "list or tuple"),
    ("qi empty", None, [], {"k": 2}, ValueError, "at least one column"),
    ("qi non-str", None, [1], {"k": 2}, TypeError, "must be a str"),
    ("qi duplicate", None, ["a", "a"], {"k": 2}, ValueError, "named more than once"),
    ("missing column", None, ["zz"], {"k": 2}, ValueError, "is not in the table"),
]


@pytest.mark.parametrize(
    ("label", "table", "qis", "kwargs", "error", "text"),
    REFUSALS,
    ids=[r[0] for r in REFUSALS],
)
def test_argument_refusals(
    label: str,
    table: Any,
    qis: Any,
    kwargs: dict[str, Any],
    error: type[Exception],
    text: str,
) -> None:
    del label
    with pytest.raises(error, match=text) as info:
        microaggregate(_frame() if table is None else table, qis, **kwargs)
    _assert_clean(info.value)


def test_duplicated_column_in_table() -> None:
    frame = pd.DataFrame([[SECRET, 1.0]] * 4, columns=["a", "a"])
    with pytest.raises(ValueError, match="appears more than once") as info:
        microaggregate(frame, ["a"], k=2)
    _assert_clean(info.value)


NON_NUMERIC: list[tuple[str, Any]] = [
    ("bool", [True, False, True, False, True, False]),
    ("object ints", pd.Series([7731, 1, 2, 3, 4, 5], dtype=object)),
    ("string", ["7731", "b", "c", "d", "e", "f"]),
    ("category", pd.Categorical(["7731", "b", "c", "d", "e", "f"])),
    ("datetime", pd.date_range("2020-01-01", periods=6)),
    ("timedelta", pd.to_timedelta(range(6), unit="D")),
    ("decimal object", [Decimal("7731.25")] * 6),
    (
        "decimal arrow",
        pd.array([Decimal("7731.25")] * 6, dtype=pd.ArrowDtype(pa.decimal128(8, 2))),
    ),
    ("complex", [complex(7731, 1)] * 6),
    ("nullable bool", pd.array([True, False] * 3, dtype="boolean")),
    ("dates", [datetime.date(1977, 3, 1)] * 6),
]


@pytest.mark.parametrize(("label", "values"), NON_NUMERIC, ids=[r[0] for r in NON_NUMERIC])
def test_non_numeric_refused(label: str, values: Any) -> None:
    del label
    with pytest.raises(TypeError, match="'q' is not a numeric column") as info:
        microaggregate(_frame(q=values), ["a", "q"], k=2)
    _assert_clean(info.value)


def test_non_numeric_arrow_refused() -> None:
    table = pa.table({"a": BASE, "q": pa.array([True, False] * 3)})
    with pytest.raises(TypeError, match="not a numeric column"):
        microaggregate(table, ["a", "q"], k=2)
    table = pa.table({"a": BASE, "q": pa.array([1, 2, 3, 1, 2, 3]).dictionary_encode()})
    with pytest.raises(TypeError, match="not a numeric column"):
        microaggregate(table, ["a", "q"], k=2)


NULLS: list[tuple[str, Any]] = [
    ("nan", [SECRET, np.nan, 2.0, np.nan, 4.0, 5.0]),
    ("none", [SECRET, None, 2.0, None, 4.0, 5.0]),
    ("pd.NA", pd.array([7731, None, 2, None, 4, 5], dtype="Int64")),
    (
        "arrow-backed",
        pd.array([SECRET, None, 2.0, None, 4.0, 5.0], dtype=pd.ArrowDtype(pa.float64())),
    ),
]


@pytest.mark.parametrize(("label", "values"), NULLS, ids=[r[0] for r in NULLS])
def test_nulls_refused(label: str, values: Any) -> None:
    del label
    with pytest.raises(ValueError, match="^2 rows have a null quasi-identifier") as info:
        microaggregate(_frame(q=values), ["a", "q"], k=2)
    _assert_clean(info.value)


def test_arrow_null_and_nan_counted_together() -> None:
    q = pa.array([SECRET, None, float("nan"), 3.0, 4.0, 5.0], type=pa.float64())
    with pytest.raises(ValueError, match="^2 rows have a null quasi-identifier") as info:
        microaggregate(pa.table({"a": BASE, "q": q}), ["a", "q"], k=2)
    _assert_clean(info.value)


@pytest.mark.parametrize("bad", [np.inf, -np.inf])
def test_non_finite_refused(bad: float) -> None:
    with pytest.raises(ValueError, match="^1 rows have a non-finite") as info:
        microaggregate(_frame(q=[SECRET, bad, 2.0, 3.0, 4.0, 5.0]), ["a", "q"], k=2)
    _assert_clean(info.value)


def test_too_large_to_standardise() -> None:
    frame = _frame(q=[1.7e308, -1.7e308, 1.7e308, -1.7e308, 1.0, SECRET])
    with pytest.raises(ValueError, match="'q' is too large to standardise") as info:
        microaggregate(frame, ["a", "q"], k=2)
    _assert_clean(info.value)


def test_huge_constant_column_passes_through() -> None:
    # Constant columns skip standardisation and averaging, so no overflow is possible.
    frame = pd.DataFrame({"a": [1.0, 2.0, 3.0, 4.0], "q": [1.7e308] * 4})
    out = microaggregate(frame, ["a", "q"], k=2).table
    assert out["q"].tolist() == [1.7e308] * 4


def test_aggregate_guard_is_value_free(monkeypatch: pytest.MonkeyPatch) -> None:
    # Unreachable with finite standardised input; the guard stops a bad report anyway.
    real = _api.group_means

    def overflowing(values: np.ndarray, groups: np.ndarray, count: int) -> np.ndarray:
        means = real(values, groups, count)
        means[0, 0] = np.inf
        return means

    monkeypatch.setattr(_api, "group_means", overflowing)
    with pytest.raises(ValueError, match="too large to aggregate") as info:
        microaggregate(_frame(), ["a"], k=2)
    _assert_clean(info.value)


@pytest.mark.parametrize("n", [1, 2])
def test_fewer_rows_than_k(n: int) -> None:
    with pytest.raises(ValueError, match="fewer rows than k") as info:
        microaggregate(pd.DataFrame({"a": [SECRET] * n}), ["a"], k=3)
    _assert_clean(info.value)


def test_max_rows_refused_before_computation(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: object) -> None:
        raise AssertionError("partition must not run")

    monkeypatch.setattr(_api, "mdav_groups", boom)
    frame = pd.DataFrame({"a": np.zeros(MAX_ROWS + 1)})
    with pytest.raises(ValueError, match=f"at most {MAX_ROWS}") as info:
        microaggregate(frame, ["a"], k=2)
    _assert_clean(info.value)
    assert MAX_ROWS == 50_000


# ----------------------------------------------------------------- accepted

ACCEPTED: list[Any] = [
    "int8",
    "int16",
    "int32",
    "int64",
    "uint8",
    "uint16",
    "uint32",
    "uint64",
    "float16",
    "float32",
    "float64",
    "Int64",
    "UInt16",
    "Float64",
    pd.ArrowDtype(pa.int32()),
    pd.ArrowDtype(pa.float64()),
]


@pytest.mark.parametrize("dtype", ACCEPTED, ids=str)
def test_accepted_pandas_dtypes(dtype: Any) -> None:
    values = pd.Series([1, 2, 3, 10, 11, 12], dtype=dtype)
    out = microaggregate(pd.DataFrame({"q": values}), ["q"], k=3).table
    assert out["q"].dtype == np.float64
    assert out["q"].tolist() == [2.0, 2.0, 2.0, 11.0, 11.0, 11.0]


@pytest.mark.parametrize(
    "arrow_type", [pa.int8(), pa.int64(), pa.uint32(), pa.float16(), pa.float32(), pa.float64()]
)
def test_accepted_arrow_types(arrow_type: pa.DataType) -> None:
    raw: list[Any] = [1, 2, 3, 10, 11, 12]
    if pa.types.is_float16(arrow_type):
        raw = [np.float16(v) for v in raw]
    column = pa.chunked_array([pa.array(raw[:2], arrow_type), pa.array(raw[2:], arrow_type)])
    out = microaggregate(pa.table({"q": column}), ["q"], k=3).table
    assert out.schema.field("q").type == pa.float64()
    assert out.column("q").to_pylist() == [2.0, 2.0, 2.0, 11.0, 11.0, 11.0]


def test_empty_table_accepted() -> None:
    frame = pd.DataFrame({"a": pd.Series([], dtype="int64"), "s": pd.Series([], dtype=object)})
    result = microaggregate(frame, ["a"], k=2)
    assert isinstance(result, MicroaggregationResult)
    assert result.table["a"].dtype == np.float64
    assert len(result.table) == 0
    report = result.report
    assert report.row_count == 0
    assert report.group_count == 0
    assert report.group_size_min is None
    assert report.group_size_max is None
    assert report.group_size_distribution == ()
    assert (report.sse, report.sst, report.information_loss) == (0.0, 0.0, 0.0)
    assert report.column_information_loss == (0.0,)


def test_empty_arrow_table_accepted() -> None:
    table = pa.table({"a": pa.array([], pa.int32())})
    out = microaggregate(table, ["a"], k=2).table
    assert out.schema.field("a").type == pa.float64()
    assert out.num_rows == 0


# ------------------------------------------------------ no mutation, fidelity


def _rich_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "name": ["a", "b", "c", "d", "e", "f", "g"],
            "x": [0, 1, 2, 3, 10, 11, 12],
            "when": pd.date_range("2021-01-01", periods=7),
            "y": [0.0, 2.0, 3.0, 1.0, 12.0, 10.0, 11.0],
            "n": pd.array([1, None, 3, None, 5, 6, 7], dtype="Int64"),
        },
        index=pd.Index([70, 71, 72, 73, 74, 75, 76], name="row"),
    )


def test_pandas_input_unchanged_and_other_columns_kept() -> None:
    frame = _rich_frame()
    before = frame.copy(deep=True)
    x_buffer = frame["x"].to_numpy()
    out = microaggregate(frame, ["x", "y"], k=2).table
    pd.testing.assert_frame_equal(frame, before, check_exact=True)
    assert out is not frame
    assert list(out.columns) == list(frame.columns)
    pd.testing.assert_index_equal(out.index, frame.index)
    for name in ("name", "when", "n"):
        pd.testing.assert_series_equal(out[name], frame[name], check_exact=True)
    assert out["x"].index.equals(frame.index)
    assert not np.shares_memory(out["x"].to_numpy(), x_buffer)


def test_arrow_input_unchanged_and_other_columns_kept() -> None:
    frame = _rich_frame()
    table = pa.Table.from_pandas(frame, preserve_index=False).replace_schema_metadata({b"k": b"v"})
    table = table.set_column(
        0, "name", pa.chunked_array([pa.array(["a", "b", "c"]), pa.array(["d", "e", "f", "g"])])
    )
    before = pa.table(table.columns, schema=table.schema)
    out = microaggregate(table, ["x", "y"], k=2).table
    assert table.equals(before, check_metadata=True)
    assert out.column_names == table.column_names
    for name in ("name", "when", "n"):
        assert out.column(name).equals(table.column(name))
    assert out.schema.metadata == {b"k": b"v"}


def test_pandas_equals_arrow() -> None:
    frame = _rich_frame()
    table = pa.Table.from_pandas(frame, preserve_index=False)
    chunked = pa.table(
        {
            "x": pa.chunked_array([pa.array([0, 1, 2]), pa.array([3, 10, 11, 12])]),
            "y": table.column("y"),
        }
    )
    from_pandas = microaggregate(frame, ["x", "y"], k=2)
    from_arrow = microaggregate(chunked, ["x", "y"], k=2)
    for name in ("x", "y"):
        assert from_pandas.table[name].tolist() == from_arrow.table.column(name).to_pylist()
    assert from_pandas.report == from_arrow.report


def test_result_is_frozen() -> None:
    result = microaggregate(_rich_frame(), ["x"], k=2)
    with pytest.raises(AttributeError):
        result.report.k = 3  # type: ignore[misc]
    with pytest.raises(AttributeError):
        result.table = None  # type: ignore[misc]

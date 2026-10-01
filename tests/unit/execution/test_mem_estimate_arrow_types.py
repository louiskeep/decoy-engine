"""Byte-estimate pricing keyed on the Arrow type (plan
docs/plans/2026-10-01-byte-estimate-temporal-columns.md, revision 3.1).

Acceptance tests 3, 5, 6, 7, 8, 11, 12 and 12b. The end-to-end tests (1, 1b, 2, 4
and the run_pipeline halves of 11 and 12) live in
tests/integration/test_byte_estimate_arrow_types_e2e.py.

Owner direction ("bound memory, don't predict pandas"): an estimate may only rise
relative to the old rules, and an uncertain representation is UNPRICEABLE. A test
here that pins a coarse over-price is pinning intended behavior.
"""

from __future__ import annotations

import datetime as dt
import decimal
import importlib
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.execution._mem_estimate import (
    _FIXED_WIDTH_DTYPE_BYTES,
    ColumnSizeSpec,
    TableSizeSpec,
    estimate_peak_bytes,
    fits,
    raw_data_bytes,
)
from decoy_engine.execution._mem_estimate_schema import (
    sample_average_string_bytes,
    table_size_spec_from_profile,
    table_size_spec_from_table,
)
from decoy_engine.execution._pipeline_routing_signals import byte_estimate_full_frame_fits
from decoy_engine.profile import profile_source
from decoy_engine.profile._readers import LazySource
from decoy_engine.profile._types import ColumnProfile, Profile, TableProfile
from tests.native._rev9_type_catalogue import CATALOGUE

_GB = 1024 * 1024 * 1024


def _arrow() -> Any:
    """The new classifier module, imported per test so a missing module fails the
    test that needs it instead of collapsing the whole file."""
    return importlib.import_module("decoy_engine.execution._mem_estimate_arrow")


def _column_arrow_types(resident: Any, profile_table: Any) -> dict[str, tuple[pa.DataType, bool]]:
    from decoy_engine.execution import _pipeline_routing_signals as signals

    return signals._column_arrow_types(resident, profile_table)  # type: ignore[attr-defined, no-any-return]


def _kind(cls: Any) -> str:
    return type(cls).__name__.lower()


def _price(cls: Any) -> float:
    """Resident bytes per row a classification implies (Fixed table lookup, a
    declared width plus the string-object overhead is not needed here)."""
    assert _kind(cls) == "fixed", cls
    return float(_FIXED_WIDTH_DTYPE_BYTES[cls.label])


def _col_profile(name: str, *, dtype: str, row_count: int, null_count: int = 0) -> ColumnProfile:
    return ColumnProfile(
        name=name,
        dtype=dtype,
        row_count=row_count,
        null_count=null_count,
        distinct_count=row_count,
        sampled=False,
        is_candidate_key_sampled=False,
        declared_pk=False,
        is_fk=False,
        fk_target=None,
        pii_class=None,
    )


class _FakeProfile:
    def __init__(self, tables: tuple[TableProfile, ...]) -> None:
        self.relationships: tuple[Any, ...] = ()
        self.tables = tables


# ---------------------------------------------------------------------------
# Test 3: catalogue, unit
# ---------------------------------------------------------------------------

_INTS = ("int8", "int16", "int32", "int64", "uint8", "uint16", "uint32", "uint64")
_FLOATS = ("float16", "float32", "float64")
_SAMPLED = (
    "string",
    "large_string",
    "string_view",
    "binary",
    "large_binary",
    "binary_view",
    "json",
    "dictionary_string",
    "dictionary_large_string",
    "dictionary_binary",
)
_UNPRICEABLE = (
    "month_day_nano_interval",
    "list",
    "large_list",
    "list_view",
    "large_list_view",
    "fixed_size_list",
    "struct",
    "map",
    "dense_union",
    "sparse_union",
    "dictionary_string_view",
    "ree_string",
    "ree_string_view",
    "fixed_shape_tensor",
)
# Wrapped numeric forms: a resident wrapper is priced as nullable whatever the
# outer null count says, so only the nullable price is pinned for them.
_WRAPPED_NUMERIC_PRICE = {
    "dictionary_int": 8.0,
    "dictionary_float16": 2.0,
    "ree_int": 8.0,
    "ree_float": 8.0,
    "bool8": 8.0,
    "opaque": 8.0,
}
_FIXED_BY_NAME: dict[str, str] = {
    "bool": "bool",
    "float16": "float16",
    "float32": "float32",
    "float64": "float64",
    "date32": "pyobject[date]",
    "date64": "pyobject[date]",
    "time32_s": "pyobject[time]",
    "time32_ms": "pyobject[time]",
    "time64_us": "pyobject[time]",
    "time64_ns": "pyobject[time]",
    "decimal32": "pyobject[decimal]",
    "decimal64": "pyobject[decimal]",
    "decimal128": "pyobject[decimal]",
    "decimal256": "pyobject[decimal]",
    **{
        f"timestamp_{u}{tz}": "datetime64[ns]"
        for u in ["s", "ms", "us", "ns"]
        for tz in ("", "_tz")
    },
    **{f"duration_{u}": "timedelta64[ns]" for u in ["s", "ms", "us", "ns"]},
}
_DECLARED = {"null": 0.0, "fixed_size_binary": 2.0, "uuid": 16.0}
_ALL_PINNED = (
    set(_INTS)
    | set(_SAMPLED)
    | set(_UNPRICEABLE)
    | set(_WRAPPED_NUMERIC_PRICE)
    | set(_FIXED_BY_NAME)
    | set(_DECLARED)
    | set(_FLOATS)
)


def test_catalogue_pin_table_covers_every_catalogue_type() -> None:
    """A new pyarrow type must be classified deliberately, not slip through."""
    unlisted = sorted(set(CATALOGUE) - _ALL_PINNED)
    assert unlisted == [], f"classify_column pin table is missing catalogue types: {unlisted}"


@pytest.mark.parametrize("has_null", [False, True])
@pytest.mark.parametrize("name", sorted(CATALOGUE))
def test_classify_column_matches_the_design_table(name: str, has_null: bool) -> None:
    mod = _arrow()
    arr = CATALOGUE[name](has_null)
    cls = mod.classify_column(arr.type, has_nulls=has_null)

    if name in _DECLARED:
        assert _kind(cls) == "declared"
        assert cls.width_bytes == _DECLARED[name]
    elif name in _SAMPLED:
        assert _kind(cls) == "sampled"
        assert cls.decoded_type in (
            pa.string(),
            pa.large_string(),
            pa.binary(),
            pa.large_binary(),
        )
    elif name in _UNPRICEABLE:
        assert _kind(cls) == "unpriceable"
        assert isinstance(cls.reason, str) and cls.reason
    elif name in _WRAPPED_NUMERIC_PRICE:
        if has_null:
            assert _price(cls) == _WRAPPED_NUMERIC_PRICE[name]
        else:
            # Unconditional for wrapped types: never below the narrow price, and
            # never a different kind.
            assert _kind(cls) == "fixed"
    elif name == "bool":
        assert _kind(cls) == "fixed"
        assert cls.label == ("pyobject[bool]" if has_null else "bool")
    elif name in _INTS:
        assert _kind(cls) == "fixed"
        assert cls.label == ("float64" if has_null else name)
    elif name in _FIXED_BY_NAME:
        assert _kind(cls) == "fixed"
        assert cls.label == _FIXED_BY_NAME[name]
    else:  # floats
        assert _kind(cls) == "fixed"
        assert cls.label == name


@pytest.mark.parametrize("has_null", [False, True])
@pytest.mark.parametrize("name", sorted(CATALOGUE))
def test_every_catalogue_column_builds_a_size_spec_without_raising(
    name: str, has_null: bool
) -> None:
    table = pa.table({"c": CATALOGUE[name](has_null)})
    spec = table_size_spec_from_table("t", table)
    assert len(spec.columns) == 1
    raw_data_bytes([spec])  # pricing a spec never raises either


def test_new_pyobject_labels_are_not_below_the_measured_cpython_cost() -> None:
    """Interpreter drift that grows an object fails here instead of under-pricing."""
    cost = {
        "pyobject[date]": 8 + sys.getsizeof(dt.date(2020, 1, 1)),
        "pyobject[time]": 8 + sys.getsizeof(dt.time(1, 1, 1)),
        "pyobject[decimal]": 8 + sys.getsizeof(decimal.Decimal("1." + "1" * 75)),
        "pyobject[bool]": 8,
    }
    for label, measured in cost.items():
        assert label in _FIXED_WIDTH_DTYPE_BYTES, label
        assert _FIXED_WIDTH_DTYPE_BYTES[label] >= measured, (label, measured)
    assert _FIXED_WIDTH_DTYPE_BYTES["float16"] == 2


# ---------------------------------------------------------------------------
# Test 5: string estimates unchanged; sampler type-check
# ---------------------------------------------------------------------------


def _mean_utf8(values: list[Any]) -> float:
    present = [v for v in values if v is not None]
    return sum(len(v.encode("utf-8")) for v in present) / len(present)


def _two_chunks(arr: pa.Array) -> pa.ChunkedArray:
    return pa.chunked_array([arr, arr])


def test_sampler_literal_pins_from_main() -> None:
    cycle = ["a", "bb", "ccc", "a", "dddd"]
    assert sample_average_string_bytes(pa.array(cycle * 40)) == pytest.approx(2.2)
    unicode_cycle = ["héllo", "日本", None, "x", "yz"]
    assert sample_average_string_bytes(pa.array(unicode_cycle * 40)) == pytest.approx(3.75)
    assert sample_average_string_bytes(pa.array(["ab", "abcd", None, "abcdef"])) == pytest.approx(
        4.0
    )
    assert sample_average_string_bytes(pa.array([None, None], type=pa.string())) == 0.0


def test_sampler_catalogue_pins() -> None:
    json_with_null = CATALOGUE["json"](True)
    assert sample_average_string_bytes(json_with_null) == pytest.approx(6.0)
    assert sample_average_string_bytes(_two_chunks(json_with_null)) == pytest.approx(6.0)
    assert sample_average_string_bytes(CATALOGUE["null"](False)) == 0.0
    for name in ("string", "large_string", "string_view"):
        assert sample_average_string_bytes(CATALOGUE[name](False)) == pytest.approx(2.2), name


@pytest.mark.parametrize("name", ["dictionary_string", "dictionary_large_string"])
@pytest.mark.parametrize("has_null", [False, True])
def test_sampler_dictionary_of_plain_string_array_and_chunked(name: str, has_null: bool) -> None:
    arr = CATALOGUE[name](has_null)
    expected = _mean_utf8(arr.to_pylist())
    assert sample_average_string_bytes(arr) == pytest.approx(expected)
    assert sample_average_string_bytes(_two_chunks(arr)) == pytest.approx(expected)


def test_sampler_matches_python_reference_on_non_ascii_text() -> None:
    values = ["é", "日本語テキスト", "😀", None, "plain"]
    assert sample_average_string_bytes(pa.array(values)) == pytest.approx(_mean_utf8(values))
    assert sample_average_string_bytes(pa.array(values, pa.large_string())) == pytest.approx(
        _mean_utf8(values)
    )


def _unsampleable_arrays() -> dict[str, pa.Array | pa.ChunkedArray]:
    dict_view = CATALOGUE["dictionary_string_view"](False)
    ree_string = CATALOGUE["ree_string"](False)
    return {
        "dictionary_string_view_array": dict_view,
        "dictionary_string_view_chunked": _two_chunks(dict_view),
        "ree_string": ree_string,
    }


@pytest.mark.parametrize("which", sorted(_unsampleable_arrays()))
def test_sampler_raises_typeerror_naming_the_type_before_any_kernel(
    which: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    arr = _unsampleable_arrays()[which]
    calls: list[str] = []

    def spy(name: str, original: Callable[..., Any]) -> Callable[..., Any]:
        def inner(*a: Any, **k: Any) -> Any:
            calls.append(name)
            return original(*a, **k)

        return inner

    for fn in ("binary_length", "sum", "drop_null", "run_end_decode"):
        monkeypatch.setattr(pc, fn, spy(fn, getattr(pc, fn)))
    with pytest.raises(TypeError) as err:
        sample_average_string_bytes(arr)
    text = str(err.value)
    assert ("dictionary" in text) or ("run_end_encoded" in text), text
    assert calls == [], f"an Arrow kernel ran before the guard: {calls}"


@pytest.mark.parametrize(
    ("array", "needle"),
    [
        (pa.array([1, 2, 3], pa.int64()), "int64"),
        (pa.array([dt.date(2020, 1, 1)], pa.date32()), "date32"),
        (pa.array([decimal.Decimal("1.50")], pa.decimal128(7, 2)), "decimal128"),
    ],
)
def test_sampler_guard_rejects_non_string_arrays_with_the_type_name(
    array: pa.Array, needle: str
) -> None:
    with pytest.raises(TypeError, match=needle):
        sample_average_string_bytes(array)


_PIN_ROWS = 200


def _pin_table() -> pa.Table:
    n = _PIN_ROWS
    return pa.table(
        {
            "s": pa.array([["a", "bb", "ccc", "a", "dddd"][i % 5] for i in range(n)]),
            "u": pa.array([["héllo", "日本", None, "x", "yz"][i % 5] for i in range(n)]),
            "i8": pa.array([i % 100 for i in range(n)], pa.int8()),
            "i8n": pa.array([None if i % 7 == 0 else i % 100 for i in range(n)], pa.int8()),
            "f": pa.array([i * 0.5 for i in range(n)]),
            "ts": pa.array(list(range(n)), pa.timestamp("us")),
            "b": pa.array([i % 2 == 0 for i in range(n)]),
        }
    )


def _pin_profile(table: pa.Table) -> TableProfile:
    labels = {
        "s": "object",
        "u": "object",
        "i8": "int8",
        "i8n": "float64",
        "f": "float64",
        "ts": "datetime64[ns]",
        "b": "bool",
    }
    return TableProfile(
        name="t",
        row_count=table.num_rows,
        columns=tuple(
            _col_profile(
                n, dtype=labels[n], row_count=table.num_rows, null_count=table.column(n).null_count
            )
            for n in table.column_names
        ),
    )


def test_pin_table_profile_adapter_prices_29190_with_the_main_labels() -> None:
    table = _pin_table()
    profile_table = _pin_profile(table)
    arrow_types = _column_arrow_types(table, profile_table)
    spec = table_size_spec_from_profile(
        profile_table,
        sample={n: table.column(n) for n in table.column_names},
        arrow_types=arrow_types,  # type: ignore[call-arg]
    )
    assert [c.dtype for c in spec.columns] == [
        "object",
        "object",
        "int8",
        "float64",
        "float64",
        "datetime64[ns]",
        "bool",
    ]
    assert raw_data_bytes([spec]).priceable_bytes == 29190


# ---------------------------------------------------------------------------
# Test 7: prepared path
# ---------------------------------------------------------------------------


def test_prepared_path_prices_the_pin_table_like_the_profile_adapter() -> None:
    spec = table_size_spec_from_table("t", _pin_table())
    assert raw_data_bytes([spec]).priceable_bytes == 29190  # was 27790


def test_prepared_path_prices_date32_as_a_python_object() -> None:
    table = pa.table({"d": pa.array([dt.date(2020, 1, 1)] * 4, pa.date32())})
    spec = table_size_spec_from_table("t", table)
    assert spec.columns[0].dtype == "pyobject[date]"
    assert raw_data_bytes([spec]).priceable_bytes == 4 * _FIXED_WIDTH_DTYPE_BYTES["pyobject[date]"]


# ---------------------------------------------------------------------------
# Lazy sources: file helpers (tests 6, 11)
# ---------------------------------------------------------------------------


def _profile_of(path: Path, name: str = "t") -> Profile:
    first = pq.read_schema(path).names[0]
    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {name: {"type": "file", "format": "parquet", "path": str(path)}},
            "targets": {name: {"type": "file", "format": "parquet", "path": str(path) + ".out"}},
            "tables": [{"name": name, "columns": [{"name": first, "strategy": "passthrough"}]}],
        }
    ).model_dump()
    return profile_source(config, seed=1)


def test_lazy_table_with_tz_duration_and_dictionary_columns_does_not_raise(tmp_path: Path) -> None:
    table = pa.table(
        {
            "tz": pa.array([1, 2, 3], pa.timestamp("us", "+05:30")),
            "dur": pa.array([1, 2, 3], pa.duration("s")),
            "cat": pa.DictionaryArray.from_arrays(
                pa.array([0, 1, 0], pa.int32()), pa.array(["a", "b"])
            ),
        }
    )
    path = tmp_path / "t.parquet"
    pq.write_table(table, path)
    profile = _profile_of(path)
    result = byte_estimate_full_frame_fits(
        profile,
        caller_sources={"t": LazySource(path)},
        table_kinds={"t": "mask"},
        budget_bytes=_GB,
    )
    # The dictionary-of-string column is sampled-or-nothing, and a lazy table has
    # nothing to sample, so the table stays UNPRICEABLE (route bounded).
    assert result is None


def test_lazy_table_of_fixed_types_prices_a_fit_without_a_sample(tmp_path: Path) -> None:
    table = pa.table(
        {
            "tz": pa.array([1, 2, 3], pa.timestamp("us", "+05:30")),
            "dur": pa.array([1, 2, 3], pa.duration("s")),
            "n": pa.array([1, 2, 3], pa.int64()),
            "d": pa.array([dt.date(2020, 1, d) for d in (1, 2, 3)], pa.date32()),
        }
    )
    path = tmp_path / "t.parquet"
    pq.write_table(table, path)
    assert (
        byte_estimate_full_frame_fits(
            _profile_of(path),
            caller_sources={"t": LazySource(path)},
            table_kinds={"t": "mask"},
            budget_bytes=_GB,
        )
        is True
    )


def test_lazy_string_column_stays_unpriceable(tmp_path: Path) -> None:
    table = pa.table({"s": pa.array(["a", "b", "c"]), "n": pa.array([1, 2, 3], pa.int64())})
    path = tmp_path / "t.parquet"
    pq.write_table(table, path)
    assert (
        byte_estimate_full_frame_fits(
            _profile_of(path),
            caller_sources={"t": LazySource(path)},
            table_kinds={"t": "mask"},
            budget_bytes=_GB,
        )
        is None
    )


# ---------------------------------------------------------------------------
# Tests 11, 12: nullability comes from metadata or the resident column, never
# from the profile sample.
# ---------------------------------------------------------------------------

_N = 50_000
_ROW_GROUP = 10_000
_NULLS_FROM = 30_000


def _late_null_table() -> pa.Table:
    """int8 and bool columns whose nulls all sit past the 10,000-row profiling
    sample, plus a control int8 declared non-nullable with no nulls."""
    i8 = [None if i >= _NULLS_FROM and i % 5 == 0 else i % 100 for i in range(_N)]
    flags = [None if i >= _NULLS_FROM and i % 5 == 0 else i % 2 == 0 for i in range(_N)]
    schema = pa.schema(
        [
            pa.field("i8", pa.int8()),
            pa.field("b", pa.bool_()),
            pa.field("ctrl", pa.int8(), nullable=False),
        ]
    )
    return pa.table(
        {
            "i8": pa.array(i8, pa.int8()),
            "b": pa.array(flags, pa.bool_()),
            "ctrl": pa.array([i % 100 for i in range(_N)], pa.int8()),
        },
        schema=schema,
    )


def _spec(dtypes: dict[str, str], rows: int = _N) -> TableSizeSpec:
    return TableSizeSpec(
        name="t",
        row_count=rows,
        columns=tuple(ColumnSizeSpec(name=n, dtype=d) for n, d in dtypes.items()),
    )


def _straddling_budget(narrow: TableSizeSpec, wide: TableSizeSpec) -> int:
    """A budget the narrow table fits and the wide one does not, computed from the
    public estimator. Both facts are asserted before the budget is used."""
    narrow_peak = estimate_peak_bytes([narrow], "full_frame").estimated_bytes
    wide_peak = estimate_peak_bytes([wide], "full_frame").estimated_bytes
    assert narrow_peak is not None and wide_peak is not None and narrow_peak < wide_peak
    budget = int((narrow_peak * 1.3 + wide_peak * 1.3) / 2)
    assert fits([narrow], "full_frame", budget) is True
    assert fits([wide], "full_frame", budget) is False
    return budget


def _budget_for_late_null_table() -> int:
    # float64 stands in for the 8-byte nullable price of both columns.
    narrow = _spec({"i8": "int8", "b": "bool", "ctrl": "int8"})
    wide = _spec({"i8": "float64", "b": "float64", "ctrl": "int8"})
    return _straddling_budget(narrow, wide)


def _labels(spec: TableSizeSpec) -> dict[str, str]:
    return {c.name: c.dtype for c in spec.columns}


def test_late_null_profile_sample_misses_every_null(tmp_path: Path) -> None:
    path = tmp_path / "t.parquet"
    pq.write_table(_late_null_table(), path, row_group_size=_ROW_GROUP)
    profile_table = _profile_of(path).tables[0]
    assert {c.name: c.null_count for c in profile_table.columns} == {"i8": 0, "b": 0, "ctrl": 0}


def test_lazy_column_null_counts_come_from_footer_statistics(tmp_path: Path) -> None:
    path = tmp_path / "t.parquet"
    pq.write_table(_late_null_table(), path, row_group_size=_ROW_GROUP)
    counts = LazySource(path).column_null_counts()
    assert counts["ctrl"] == 0
    assert (
        counts["i8"] == counts["b"] == sum(1 for i in range(_N) if i >= _NULLS_FROM and i % 5 == 0)
    )


def test_lazy_column_null_counts_are_none_without_statistics(tmp_path: Path) -> None:
    path = tmp_path / "t.parquet"
    pq.write_table(_late_null_table(), path, row_group_size=_ROW_GROUP, write_statistics=False)
    assert LazySource(path).column_null_counts() == {"i8": None, "b": None, "ctrl": None}


def test_lazy_column_null_counts_zero_row_groups_means_no_nulls(tmp_path: Path) -> None:
    path = tmp_path / "t.parquet"
    pq.ParquetWriter(path, pa.schema([pa.field("x", pa.int8())])).close()
    assert pq.read_metadata(path).num_row_groups == 0
    assert LazySource(path).column_null_counts() == {"x": 0}


def test_lazy_column_null_counts_empty_row_group_means_no_nulls(tmp_path: Path) -> None:
    path = tmp_path / "t.parquet"
    pq.write_table(pa.table({"x": pa.array([], pa.int8())}), path)
    assert LazySource(path).column_null_counts() == {"x": 0}


def test_lazy_nullable_columns_price_wide_and_the_required_control_stays_narrow(
    tmp_path: Path,
) -> None:
    path = tmp_path / "t.parquet"
    pq.write_table(_late_null_table(), path, row_group_size=_ROW_GROUP)
    profile_table = _profile_of(path).tables[0]
    types = _column_arrow_types(LazySource(path), profile_table)
    assert types["i8"] == (pa.int8(), True)
    assert types["b"] == (pa.bool_(), True)
    assert types["ctrl"] == (pa.int8(), False)
    spec = table_size_spec_from_profile(profile_table, arrow_types=types)  # type: ignore[call-arg]
    assert _labels(spec) == {"i8": "float64", "b": "pyobject[bool]", "ctrl": "int8"}


def test_lazy_without_statistics_falls_back_to_the_nullable_flag(tmp_path: Path) -> None:
    path = tmp_path / "t.parquet"
    pq.write_table(_late_null_table(), path, row_group_size=_ROW_GROUP, write_statistics=False)
    profile_table = _profile_of(path).tables[0]
    types = _column_arrow_types(LazySource(path), profile_table)
    assert types["i8"][1] is True and types["b"][1] is True
    assert types["ctrl"][1] is False  # nullable=False proves no nulls


def test_lazy_late_null_columns_flip_the_verdict_under_a_tight_budget(tmp_path: Path) -> None:
    budget = _budget_for_late_null_table()
    path = tmp_path / "t.parquet"
    pq.write_table(_late_null_table(), path, row_group_size=_ROW_GROUP)
    profile = _FakeProfile((_profile_of(path).tables[0],))
    assert (
        byte_estimate_full_frame_fits(
            profile,
            caller_sources={"t": LazySource(path)},
            table_kinds={"t": "mask"},
            budget_bytes=budget,
        )
        is False
    )


def test_resident_nullability_comes_from_the_column_not_the_profile_sample(
    tmp_path: Path,
) -> None:
    budget = _budget_for_late_null_table()
    table = _late_null_table()
    path = tmp_path / "t.parquet"
    pq.write_table(table, path, row_group_size=_ROW_GROUP)
    profile_table = _profile_of(path).tables[0]
    assert all(c.null_count == 0 for c in profile_table.columns)  # the sample missed them
    types = _column_arrow_types(table, profile_table)
    assert types["i8"] == (pa.int8(), True)
    assert types["b"] == (pa.bool_(), True)
    assert types["ctrl"] == (pa.int8(), False)
    spec = table_size_spec_from_profile(
        profile_table,
        sample={n: table.column(n) for n in table.column_names},
        arrow_types=types,  # type: ignore[call-arg]
    )
    assert _labels(spec) == {"i8": "float64", "b": "pyobject[bool]", "ctrl": "int8"}
    prepared = table_size_spec_from_table("t", table)
    assert _labels(prepared) == {"i8": "float64", "b": "pyobject[bool]", "ctrl": "int8"}
    assert (
        byte_estimate_full_frame_fits(
            _FakeProfile((profile_table,)),
            caller_sources={"t": table},
            table_kinds={"t": "mask"},
            budget_bytes=budget,
        )
        is False
    )


class _Int8Ext(pa.ExtensionType):
    def __init__(self) -> None:
        super().__init__(pa.int8(), "decoy_test.int8_ext")

    def __arrow_ext_serialize__(self) -> bytes:
        return b""

    @classmethod
    def __arrow_ext_deserialize__(cls, storage_type: pa.DataType, serialized: bytes) -> Any:
        return cls()


class _BoolExt(pa.ExtensionType):
    def __init__(self) -> None:
        super().__init__(pa.bool_(), "decoy_test.bool_ext")

    def __arrow_ext_serialize__(self) -> bytes:
        return b""

    @classmethod
    def __arrow_ext_deserialize__(cls, storage_type: pa.DataType, serialized: bytes) -> Any:
        return cls()


def _hidden_null_dictionary(values: pa.Array) -> pa.Array:
    return pa.DictionaryArray.from_arrays(pa.array([0, 1, 0, 0, 0, 0], pa.int32()), values)


def _ree(values: pa.Array, run_ends: list[int]) -> pa.Array:
    return pa.RunEndEncodedArray.from_arrays(pa.array(run_ends, pa.int32()), values)


def _wrapped_columns() -> dict[str, tuple[pa.Array, str]]:
    """column -> (array, 8-byte label it must classify as). Each outer column
    reports null_count == 0 (asserted by the test) while its logical content may
    hold a null, or, for the no-null cases, holds none."""
    return {
        "dict_i8": (_hidden_null_dictionary(pa.array([5, None], pa.int8())), "float64"),
        "dict_bool": (
            _hidden_null_dictionary(pa.array([True, None], pa.bool_())),
            "pyobject[bool]",
        ),
        "ext_i8": (
            pa.ExtensionArray.from_storage(_Int8Ext(), pa.array([1, 2, 3, 4, 5, 6], pa.int8())),
            "float64",
        ),
        "ext_bool": (
            pa.ExtensionArray.from_storage(_BoolExt(), pa.array([True] * 6, pa.bool_())),
            "pyobject[bool]",
        ),
        "ree_i8_nullrun": (_ree(pa.array([1, None, 2], pa.int8()), [2, 4, 6]), "float64"),
        "ree_bool_nullrun": (
            _ree(pa.array([True, None, False], pa.bool_()), [2, 4, 6]),
            "pyobject[bool]",
        ),
        "ree_i8_nonull": (_ree(pa.array([1, 2], pa.int8()), [3, 6]), "float64"),
    }


@pytest.mark.parametrize("name", sorted(_wrapped_columns()))
def test_resident_top_level_wrappers_price_as_nullable_at_both_call_sites(name: str) -> None:
    array, label = _wrapped_columns()[name]
    table = pa.table({"c": array})
    assert table.column("c").null_count == 0, "the outer column must hide any logical null"

    prepared = table_size_spec_from_table("t", table)
    assert prepared.columns[0].dtype == label

    profile_table = TableProfile(
        name="t", row_count=6, columns=(_col_profile("c", dtype="int8", row_count=6),)
    )
    types = _column_arrow_types(table, profile_table)
    assert types["c"][1] is True
    adapted = table_size_spec_from_profile(
        profile_table,
        sample={"c": table.column("c")},
        arrow_types=types,  # type: ignore[call-arg]
    )
    assert adapted.columns[0].dtype == label


@pytest.mark.parametrize("name", sorted(_wrapped_columns()))
def test_resident_wrapper_tight_budget_verdict_uses_the_wide_price(name: str) -> None:
    array, label = _wrapped_columns()[name]
    rows = 50_000
    table = pa.table({"c": pa.chunked_array([array] * (rows // len(array)))})
    narrow = _spec({"c": "int8" if label == "float64" else "bool"}, rows)
    wide = _spec({"c": "float64"}, rows)
    budget = _straddling_budget(narrow, wide)
    profile_table = TableProfile(
        name="t", row_count=rows, columns=(_col_profile("c", dtype="int8", row_count=rows),)
    )
    assert (
        byte_estimate_full_frame_fits(
            _FakeProfile((profile_table,)),
            caller_sources={"t": table},
            table_kinds={"t": "mask"},
            budget_bytes=budget,
        )
        is False
    )


# ---------------------------------------------------------------------------
# Test 12b: no Arrow type at all
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Int8", "float64"),
        ("UInt16", "float64"),
        ("boolean", "pyobject[bool]"),
        ("int8", "float64"),
        ("uint16", "float64"),
        ("bool", "pyobject[bool]"),
        ("int32", "float64"),
        ("UInt32", "float64"),
    ],
)
def test_no_arrow_type_widens_labels_that_can_carry_a_null_mask(label: str, expected: str) -> None:
    profile_table = TableProfile(
        name="t",
        row_count=10,
        columns=(_col_profile("x", dtype=label, row_count=10, null_count=0),),
    )
    spec = table_size_spec_from_profile(profile_table, arrow_types=None)  # type: ignore[call-arg]
    assert spec.columns[0].dtype == expected
    assert _FIXED_WIDTH_DTYPE_BYTES[spec.columns[0].dtype] == 8
    assert raw_data_bytes([spec]).priceable_bytes == 80  # not the revision-2 1, 2 or 1 bytes/row


@pytest.mark.parametrize("label", ["int64", "float64", "float32", "datetime64[ns]"])
def test_no_arrow_type_keeps_labels_that_cannot_hide_a_null_mask(label: str) -> None:
    profile_table = TableProfile(
        name="t", row_count=10, columns=(_col_profile("x", dtype=label, row_count=10),)
    )
    spec = table_size_spec_from_profile(profile_table)
    assert spec.columns[0].dtype == label


@pytest.mark.parametrize("label", ["category", "timedelta64[s]", "weird"])
def test_no_arrow_type_unknown_label_is_unpriceable_never_an_error(label: str) -> None:
    profile_table = TableProfile(
        name="t", row_count=10, columns=(_col_profile("x", dtype=label, row_count=10),)
    )
    spec = table_size_spec_from_profile(profile_table)
    assert spec.columns[0].unpriceable
    assert raw_data_bytes([spec]).priceable_bytes == 0


def test_the_adapter_still_honors_declared_widths_over_arrow_types() -> None:
    profile_table = TableProfile(
        name="t", row_count=5, columns=(_col_profile("s", dtype="object", row_count=5),)
    )
    spec = table_size_spec_from_profile(
        profile_table,
        declared_widths={"s": 20.0},
        arrow_types={"s": (pa.string(), False)},  # type: ignore[call-arg]
    )
    assert spec.columns[0].string_width_bytes == 20.0


def test_resident_arrow_type_wins_over_a_disagreeing_profile_label() -> None:
    profile_table = TableProfile(
        name="t", row_count=3, columns=(_col_profile("c", dtype="object", row_count=3),)
    )
    resident = pa.table({"c": pa.array([1, 2, 3], pa.int64())})
    types = _column_arrow_types(resident, profile_table)
    spec = table_size_spec_from_profile(
        profile_table,
        sample={"c": resident.column("c")},
        arrow_types=types,  # type: ignore[call-arg]
    )
    assert spec.columns[0].dtype == "int64"
    assert (
        byte_estimate_full_frame_fits(
            _FakeProfile((profile_table,)),
            caller_sources={"t": resident},
            table_kinds={"t": "mask"},
            budget_bytes=_GB,
        )
        is True
    )


def test_arrow_type_mapping_tolerates_a_namespace_profile_table() -> None:
    # The route-kill spies pass SimpleNamespace profile tables.
    profile_table = SimpleNamespace(name="m", columns=[SimpleNamespace(name="x")])
    types = _column_arrow_types(pa.table({"x": [1, 2, 3]}), profile_table)
    assert types == {"x": (pa.int64(), False)}

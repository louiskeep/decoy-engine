"""B8 acceptance tests 3 and 4: unconfigured columns of every Arrow type.

Test 3 runs the whole type catalogue, with and without a null, as an unconfigured column
beside redact, truncate and hash columns on the native route and on the forced oracle
route. Test 4 puts more than thirty unusual columns in one table. The column must come
back as the source column and field in both runs; a type that diverges stops the build for
a plan revision, it is not filtered out.
"""

from __future__ import annotations

import datetime as dt
import decimal
from typing import Any

import pyarrow as pa
import pytest

from tests.native._b8_support import (
    Run,
    assert_same_as_oracle,
    identical,
    run_pair,
    same_field,
)
from tests.native._chunked_entry_support import (
    NEEDS_COMPANION,
    faker_col,
    hash_col,
    redact,
    truncate,
)
from tests.native._rev9_type_catalogue import CATALOGUE

_PARAMS = [(name, null) for name in CATALOGUE for null in (False, True)]
_IDS = [f"{name}-{'null' if null else 'nonull'}" for name, null in _PARAMS]


def _strs(values: list[str | None]) -> dict[str, pa.Array]:
    return {
        "s": pa.array(values, pa.string()),
        "t": pa.array([None if v is None else v * 2 for v in values], pa.string()),
        "h": pa.array(values, pa.string()),
    }


def _source(arr: pa.Array, values: list[str | None]) -> pa.Table:
    return pa.table({**_strs(values), "x": arr})


def _x_column(run: Run) -> list[pa.ChunkedArray]:
    return [t.column("x") for t in run.out]


@NEEDS_COMPANION
@pytest.mark.parametrize(("name", "null"), _PARAMS, ids=_IDS)
def test_type_catalogue_as_an_unconfigured_column(name: str, null: bool) -> None:
    arr = CATALOGUE[name](null)
    names: list[str | None] = ["a", "b", "c", "d", "e"]
    full = _source(arr, names)
    empty = pa.Table.from_batches([], schema=full.schema)
    chunks = [full, empty, full]
    native, forced = run_pair([redact("s"), truncate("t"), hash_col("h")], chunks)
    assert_same_as_oracle(native, forced)
    for out, src in zip(native.out, chunks, strict=True):
        assert same_field(out.schema.field("x"), src.schema.field("x"))
        assert identical(out.select(["x"]), src.select(["x"]))


# ---------------------------------------------------------------------------
# Test 4
# ---------------------------------------------------------------------------

_SIZES = (5, 3, 4, 1, 4)
_I64MIN = -(2**63)
_I64MAX = 2**63 - 1


def _per_chunk(make: Any) -> list[pa.Array]:
    return [make(i, n) for i, n in enumerate(_SIZES)]


def _vals(n: int, i: int, fn: Any, null_at: int | None = 1) -> list[Any]:
    return [
        None if (null_at is not None and j == null_at and n > 1) else fn(i, j) for j in range(n)
    ]


def _dict_array(i: int, n: int, values: list[Any], index_type: Any = None) -> pa.Array:
    index_type = index_type or pa.int32()
    idx = [None if j == 1 else j % len(values) for j in range(n)]
    return pa.DictionaryArray.from_arrays(pa.array(idx, index_type), pa.array(values))


def _unusual() -> dict[str, list[pa.Array]]:
    cols: dict[str, list[pa.Array]] = {}
    for width in ("int8", "int16", "int32", "int64", "uint8", "uint16", "uint32", "uint64"):
        t = getattr(pa, width)()
        cols[f"int_{width}"] = _per_chunk(
            lambda i, n, t=t: pa.array(_vals(n, i, lambda i, j: j), t)
        )
    cols["uint64_max"] = _per_chunk(
        lambda i, n: pa.array([2**64 - 1 - j for j in range(n)], pa.uint64())
    )
    cols["int64_above_2_53"] = _per_chunk(
        lambda i, n: pa.array(_vals(n, i, lambda i, j: 2**53 + 1 + j), pa.int64())
    )
    cols["float16"] = _per_chunk(lambda i, n: pa.array([1.5] * n, pa.float16()))
    cols["float_nan_inf"] = _per_chunk(
        lambda i, n: pa.array(
            [[float("nan"), float("inf"), float("-inf"), -0.0, 1.5][j % 5] for j in range(n)],
            pa.float64(),
        )
    )
    cols["large_string"] = _per_chunk(
        lambda i, n: pa.array([f"l{j}" for j in range(n)], pa.large_string())
    )
    cols["string_view"] = _per_chunk(
        lambda i, n: pa.array([f"v{j}" for j in range(n)], pa.string_view())
    )
    cols["binary_view"] = _per_chunk(
        lambda i, n: pa.array([b"b%d" % j for j in range(n)], pa.binary_view())
    )
    cols["fixed_size_binary"] = _per_chunk(
        lambda i, n: pa.array([b"%02d" % j for j in range(n)], pa.binary(2))
    )
    cols["decimal128_38_10"] = _per_chunk(
        lambda i, n: pa.array(
            [decimal.Decimal(f"{10**27 + j}.0123456789") for j in range(n)], pa.decimal128(38, 10)
        )
    )
    cols["decimal256_1e60"] = _per_chunk(
        lambda i, n: pa.array([decimal.Decimal(10**60 + j) for j in range(n)], pa.decimal256(76, 0))
    )
    cols["date32_far"] = _per_chunk(
        lambda i, n: pa.array([2**31 - 1 - j for j in range(n)], pa.date32())
    )
    cols["date32_ordinary"] = _per_chunk(
        lambda i, n: pa.array([dt.date(2020, 1, 1 + j) for j in range(n)], pa.date32())
    )
    cols["date64_far"] = _per_chunk(
        lambda i, n: pa.array([2**62 - j * 86400000 for j in range(n)], pa.date64())
    )
    cols["time64_ns_unaligned"] = _per_chunk(
        lambda i, n: pa.array([1000 * j + 1 for j in range(n)], pa.time64("ns"))
    )
    for unit in ("s", "ms", "us", "ns"):
        for tz in (None, "America/New_York", "+05:30"):
            t = pa.timestamp(unit, tz=tz)
            cols[f"ts_{unit}_{tz or 'naive'}"] = _per_chunk(
                lambda i, n, t=t: pa.array(
                    [_I64MIN, _I64MAX, 0, _I64MIN + 1, 7][:n] + [0] * max(0, n - 5), t
                )
            )
        d = pa.duration(unit)
        cols[f"dur_{unit}"] = _per_chunk(
            lambda i, n, d=d: pa.array([_I64MIN, _I64MAX, 0, 1, -1][:n] + [0] * max(0, n - 5), d)
        )
    cols["month_day_nano"] = _per_chunk(
        lambda i, n: pa.array(
            [pa.MonthDayNano([1, 2, j]) for j in range(n)], pa.month_day_nano_interval()
        )
    )
    for itype in (pa.int8(), pa.int32(), pa.uint64()):
        cols[f"dict_str_{itype}"] = _per_chunk(
            lambda i, n, itype=itype: _dict_array(i, n, [f"d{i}{k}" for k in range(3)], itype)
        )
    cols["dict_null_in_dictionary"] = _per_chunk(lambda i, n: _dict_array(i, n, ["a", None, "c"]))
    cols["dict_duplicate"] = _per_chunk(lambda i, n: _dict_array(i, n, ["a", "a", "b"]))
    cols["dict_nan"] = _per_chunk(lambda i, n: _dict_array(i, n, [1.0, float("nan"), 2.0]))
    cols["dict_differs_per_chunk"] = _per_chunk(
        lambda i, n: _dict_array(i, n, [f"chunk{i}-{k}" for k in range(i + 2)])
    )
    cols["list_int"] = _per_chunk(
        lambda i, n: pa.array([[j, j + 1] for j in range(n)], pa.list_(pa.int64()))
    )
    cols["struct"] = _per_chunk(
        lambda i, n: pa.array(
            [{"a": j, "b": f"s{j}"} for j in range(n)],
            pa.struct([("a", pa.int64()), ("b", pa.string())]),
        )
    )
    cols["map"] = _per_chunk(
        lambda i, n: pa.array([[(f"k{j}", j)] for j in range(n)], pa.map_(pa.string(), pa.int64()))
    )
    cols["union"] = _per_chunk(_dense_union)
    cols["extension_uuid"] = _per_chunk(
        lambda i, n: pa.ExtensionArray.from_storage(
            pa.uuid(), pa.array([bytes([j]) * 16 for j in range(n)], pa.binary(16))
        )
    )
    cols["field_metadata"] = _per_chunk(lambda i, n: pa.array([f"m{j}" for j in range(n)]))
    cols["non_nullable"] = _per_chunk(lambda i, n: pa.array(list(range(n)), pa.int32()))
    return cols


def _dense_union(i: int, n: int) -> pa.Array:
    types = pa.array([j % 2 for j in range(n)], pa.int8())
    offsets = pa.array([j // 2 for j in range(n)], pa.int32())
    ints = pa.array(list(range((n + 1) // 2)), pa.int64())
    strs = pa.array([f"u{j}" for j in range(n // 2 + 1)], pa.string())
    return pa.UnionArray.from_dense(types, offsets, [ints, strs])


def _wide_chunks() -> tuple[list[pa.Table], pa.Schema]:
    unusual = _unusual()
    fields: list[pa.Field] = [
        pa.field("s", pa.string()),
        pa.field("t", pa.string()),
        pa.field("h", pa.string()),
        pa.field("f", pa.string()),
    ]
    for name, arrays in unusual.items():
        field = pa.field(name, arrays[0].type)
        if name == "field_metadata":
            field = field.with_metadata({b"owner": b"b8"})
        if name == "non_nullable":
            field = field.with_nullable(False)
        fields.append(field)
    schema = pa.schema(fields)
    chunks = []
    for i, n in enumerate(_SIZES):
        arrays = [
            pa.array([f"s{i}{j}" for j in range(n)]),
            pa.array([f"abcdef{i}{j}" for j in range(n)]),
            pa.array([f"h{i}{j}" for j in range(n)]),
            pa.array([f"f{i}{j}" for j in range(n)]),
            *[arrs[i] for arrs in unusual.values()],
        ]
        chunks.append(pa.Table.from_arrays(arrays, schema=schema))
    return chunks, schema


@NEEDS_COMPANION
def test_many_unusual_columns_in_one_table() -> None:
    chunks, schema = _wide_chunks()
    unconfigured = sorted(set(schema.names) - {"s", "t", "h", "f"})
    assert len(unconfigured) >= 30
    native, forced = run_pair([redact("s"), truncate("t"), hash_col("h"), faker_col("f")], chunks)
    assert_same_as_oracle(native, forced)
    for result, out, src in zip(native.sink, native.out, chunks, strict=True):
        (warning,) = result.warnings
        assert warning.detail["undeclared_columns"] == unconfigured
        for name in unconfigured:
            assert same_field(out.schema.field(name), src.schema.field(name)), name
            assert identical(out.select([name]), src.select([name])), name

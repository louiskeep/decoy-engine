"""Every pyarrow type factory, as a column with and without a null (gate round 1, B2/M1).

A factory a given pyarrow lacks is skipped, so the same catalogue runs on pyarrow 24 and 25."""

from __future__ import annotations

import datetime as dt
import decimal
import uuid
from collections.abc import Callable

import pyarrow as pa

N = 5


def _vals(values: list, null: bool) -> list:
    return [None if (null and i == 1) else v for i, v in enumerate(values)]


def _simple(t: Callable[[], pa.DataType], values: list) -> Callable[[bool], pa.Array]:
    return lambda null: pa.array(_vals(values, null), t())


def _ext(
    make_type: Callable[[], pa.DataType], storage_type: Callable[[], pa.DataType], values: list
):
    def build(null: bool) -> pa.Array:
        return pa.ExtensionArray.from_storage(
            make_type(), pa.array(_vals(values, null), storage_type())
        )

    return build


def _dict(value_type: Callable[[], pa.DataType], values: list) -> Callable[[bool], pa.Array]:
    def build(null: bool) -> pa.Array:
        idx = pa.array([0, None if null else 1, 0, 1, 0], pa.int32())
        return pa.DictionaryArray.from_arrays(idx, pa.array(values, value_type()))

    return build


def _ree(value_type: Callable[[], pa.DataType], values: list) -> Callable[[bool], pa.Array]:
    def build(null: bool) -> pa.Array:
        vs = pa.array([values[0], None if null else values[1]], value_type())
        return pa.RunEndEncodedArray.from_arrays(pa.array([2, 5], pa.int32()), vs)

    return build


def _union(dense: bool) -> Callable[[bool], pa.Array]:
    def build(null: bool) -> pa.Array:
        types = pa.array([0, 1, 0, 1, 0], pa.int8())
        ints = pa.array([1, None if null else 2, 3], pa.int64())
        strs = pa.array(["a", "b"], pa.string())
        if dense:
            return pa.UnionArray.from_dense(
                types, pa.array([0, 0, 1, 1, 2], pa.int32()), [ints, strs]
            )
        return pa.UnionArray.from_sparse(
            types,
            [pa.array([1, None if null else 2, 3, 4, 5], pa.int64()), pa.array(list("abcde"))],
        )

    return build


_D = decimal.Decimal
_I = [1, 2, 3, 4, 5]
_F = [1.5, 2.5, 0.0, -0.0, 3.5]
_S = ["a", "bb", "ccc", "a", "dddd"]
_B = [b"a", b"bb", b"ccc", b"a", b"dddd"]
_TS = [1, 2, 3, 4, 5]
_UU = [uuid.UUID(int=i + 1).bytes for i in range(N)]


def _catalogue() -> dict[str, Callable[[bool], pa.Array]]:
    c: dict[str, Callable[[bool], pa.Array]] = {}

    def add(name: str, build: Callable[[bool], pa.Array]) -> None:
        try:
            build(False)
            build(True)
        except (AttributeError, TypeError, pa.ArrowException):
            return
        c[name] = build

    add("null", lambda null: pa.nulls(N))
    add("bool", _simple(pa.bool_, [True, False, True, True, False]))
    for w in ("int8", "int16", "int32", "int64", "uint8", "uint16", "uint32", "uint64"):
        add(w, _simple(getattr(pa, w), _I))
    for w in ("float16", "float32", "float64"):
        add(w, _simple(getattr(pa, w), [1.5, 2.5, 0.0, 3.5, 4.5] if w == "float16" else _F))
    for name, vals in (("string", _S), ("large_string", _S), ("string_view", _S)):
        add(name, _simple(getattr(pa, name), vals))
    for name in ("binary", "large_binary", "binary_view"):
        add(name, _simple(getattr(pa, name), _B))
    add("fixed_size_binary", _simple(lambda: pa.binary(2), [b"aa", b"bb", b"cc", b"aa", b"dd"]))
    add("date32", _simple(pa.date32, [dt.date(2020, 1, d) for d in (1, 2, 3, 1, 5)]))
    add("date64", _simple(pa.date64, [dt.date(2020, 1, d) for d in (1, 2, 3, 1, 5)]))
    add("time32_s", _simple(lambda: pa.time32("s"), [dt.time(1, 1, i) for i in range(N)]))
    add("time32_ms", _simple(lambda: pa.time32("ms"), [dt.time(1, 1, i) for i in range(N)]))
    add("time64_us", _simple(lambda: pa.time64("us"), [dt.time(1, 1, i) for i in range(N)]))
    add("time64_ns", _simple(lambda: pa.time64("ns"), _TS))
    for unit in ("s", "ms", "us", "ns"):
        add(f"timestamp_{unit}", _simple(lambda u=unit: pa.timestamp(u), _TS))
        add(f"timestamp_{unit}_tz", _simple(lambda u=unit: pa.timestamp(u, "+05:30"), _TS))
        add(f"duration_{unit}", _simple(lambda u=unit: pa.duration(u), _TS))
    add(
        "month_day_nano_interval",
        _simple(pa.month_day_nano_interval, [pa.MonthDayNano([1, 2, i]) for i in range(N)]),
    )
    dec = [_D("1.50"), _D("2.50"), _D("3.50"), _D("1.50"), _D("4.50")]
    for name in ("decimal32", "decimal64", "decimal128", "decimal256"):
        add(name, _simple(lambda n=name: getattr(pa, n)(7, 2), dec))
    add("list", _simple(lambda: pa.list_(pa.int64()), [[1], [2], [3], [1], [5]]))
    add("large_list", _simple(lambda: pa.large_list(pa.int64()), [[1], [2], [3], [1], [5]]))
    add("list_view", _simple(lambda: pa.list_view(pa.int64()), [[1], [2], [3], [1], [5]]))
    add(
        "large_list_view",
        _simple(lambda: pa.large_list_view(pa.int64()), [[1], [2], [3], [1], [5]]),
    )
    add("fixed_size_list", _simple(lambda: pa.list_(pa.int64(), 1), [[1], [2], [3], [1], [5]]))
    add("struct", _simple(lambda: pa.struct([("a", pa.int64())]), [{"a": i} for i in _I]))
    add(
        "map",
        _simple(lambda: pa.map_(pa.string(), pa.int64()), [[("a", i)] for i in _I]),
    )
    add("dense_union", _union(True))
    add("sparse_union", _union(False))
    add("dictionary_string", _dict(pa.string, ["a", "b"]))
    add("dictionary_large_string", _dict(pa.large_string, ["a", "b"]))
    add("dictionary_string_view", _dict(pa.string_view, ["a", "b"]))
    add("dictionary_int", _dict(pa.int64, [10, 20]))
    add("dictionary_float16", _dict(pa.float16, [1.5, 2.5]))
    add("dictionary_binary", _dict(pa.binary, [b"a", b"b"]))
    add("ree_string", _ree(pa.string, ["a", "b"]))
    add("ree_int", _ree(pa.int64, [10, 20]))
    add("ree_float", _ree(pa.float64, [1.5, 2.5]))
    add("ree_string_view", _ree(pa.string_view, ["a", "b"]))
    add("json", _ext(pa.json_, pa.string, ['{"a":1}', '{"a":2}', '{"a":3}', '{"a":1}', "[1]"]))
    add("uuid", _ext(pa.uuid, lambda: pa.binary(16), _UU))
    add("bool8", _ext(pa.bool8, pa.int8, [1, 0, 1, 1, 0]))
    add(
        "fixed_shape_tensor",
        _ext(
            lambda: pa.fixed_shape_tensor(pa.int32(), [2]),
            lambda: pa.list_(pa.int32(), 2),
            [[1, 2], [3, 4], [5, 6], [1, 2], [7, 8]],
        ),
    )
    add(
        "opaque",
        _ext(lambda: pa.opaque(pa.int64(), "t", "v"), pa.int64, _I),
    )
    return c


CATALOGUE: dict[str, Callable[[bool], pa.Array]] = _catalogue()

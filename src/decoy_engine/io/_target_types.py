"""The Arrow-to-Avro and Arrow-to-ORC write tables: which column types each format writes, and
the lossless cast each needs first.

Pattern: fail closed over a declared writable domain (as `write_fixed_width` does): a type with
no row is refused before any file is opened; a value that a cast would change (a `uint64` above
int64, a nanosecond timestamp with sub-microsecond digits, an ORC timestamp outside the
nanosecond range pyarrow reads back) is refused, naming the column and type, never the value.
The ORC range rule exists because pyarrow reads any ORC timestamp as `timestamp[ns]` and wraps a
value outside 1677-09-21..2262-04-11 silently.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

import pyarrow as pa
import pyarrow.compute as pc

from decoy_engine.io import avro_schema
from decoy_engine.io.format_errors import TargetColumnTypeUnsupported, TargetSchemaMismatch

Cast = Callable[[pa.ChunkedArray], pa.ChunkedArray]
_INT64_MIN, _INT64_MAX = -(2**63), 2**63 - 1
_NS_PER = {"s": 1_000_000_000, "ms": 1_000_000, "us": 1_000, "ns": 1}
_UTC = frozenset({"UTC", "Etc/UTC", "+00:00"})


class _Unwritable(TargetColumnTypeUnsupported):
    """Internal: a value the cast would change; mapped to a coded refusal after the handler."""


@dataclass(frozen=True)
class TargetPlan:
    """The write schema, one cast per column (in schema order) and the columns Avro must label
    `timestamp-*` (UTC instants written as their UTC wall clock)."""

    schema: pa.Schema
    casts: tuple[Cast, ...]
    utc_columns: frozenset[str]


def resolve_chunks(
    data: pa.Table | Iterable[pa.Table], schema: pa.Schema | None
) -> tuple[Iterable[pa.Table], pa.Schema]:
    """The chunks and the write schema. An iterable needs `schema=` (the source schema): a
    chunk's own types can be narrower than the column's (a decimal's precision follows its
    values on the pandas route), so the first chunk is not a safe write schema."""
    if isinstance(data, pa.Table):
        return [data], schema if schema is not None else data.schema
    if schema is None:
        raise TypeError("write_avro/write_orc: an iterable of chunks needs schema=")
    return data, schema


def _same(a: pa.ChunkedArray) -> pa.ChunkedArray:
    return a


def _to(t: pa.DataType) -> Cast:
    def cast(a: pa.ChunkedArray) -> pa.ChunkedArray:
        failed = False
        try:
            out = a.cast(t, safe=True)
        except (pa.ArrowInvalid, OverflowError):
            failed = True
        if failed:
            raise _Unwritable
        return out

    return cast


def _then(first: Cast, second: Cast) -> Cast:
    return lambda a: second(first(a))


def _decode(a: pa.ChunkedArray) -> pa.ChunkedArray:
    return pa.chunked_array([c.dictionary_decode() for c in a.chunks], type=a.type.value_type)


def _naive(unit: str) -> Cast:
    # A UTC-aware column as its UTC wall clock: the same integers, no zone.
    return lambda a: a.cast(pa.int64()).cast(pa.timestamp(unit))


def _in_ns_range(unit: str) -> Cast:
    per = _NS_PER[unit]
    lo, hi = -(-_INT64_MIN // per), _INT64_MAX // per

    def check(a: pa.ChunkedArray) -> pa.ChunkedArray:
        bounds = pc.min_max(a.cast(pa.int64()))  # type: ignore[attr-defined, unused-ignore]
        low, high = bounds["min"].as_py(), bounds["max"].as_py()
        if low is not None and (low < lo or high > hi):
            raise _Unwritable
        return a

    return check


_INT_WIDEN_AVRO = {
    pa.int8(): pa.int32(),
    pa.int16(): pa.int32(),
    pa.uint8(): pa.int32(),
    pa.uint16(): pa.int32(),
    pa.uint32(): pa.int64(),
    pa.uint64(): pa.int64(),
}
_INT_WIDEN_ORC = {
    pa.uint8(): pa.int16(),
    pa.uint16(): pa.int32(),
    pa.uint32(): pa.int64(),
    pa.uint64(): pa.int64(),
}
_AS_IS_AVRO = (
    pa.types.is_boolean,
    lambda t: t in (pa.int32(), pa.int64(), pa.float32(), pa.float64()),
    pa.types.is_string,
    pa.types.is_large_string,
    pa.types.is_binary,
    pa.types.is_large_binary,
    pa.types.is_date32,
    lambda t: pa.types.is_decimal128(t) and t.precision <= 38,
)
_AS_IS_ORC = (
    pa.types.is_boolean,
    lambda t: t in (pa.int8(), pa.int16(), pa.int32(), pa.int64(), pa.float32(), pa.float64()),
    pa.types.is_string,
    pa.types.is_large_string,
    pa.types.is_binary,
    pa.types.is_large_binary,
    pa.types.is_date32,
    pa.types.is_decimal128,
)


def _avro_cast(t: pa.DataType) -> tuple[Cast, bool] | None:
    """(cast, is a UTC timestamp) for one Avro column type, or None when unwritable."""
    if pa.types.is_dictionary(t):
        inner = _avro_cast(t.value_type)
        return None if inner is None else (_then(_decode, inner[0]), inner[1])
    if any(rule(t) for rule in _AS_IS_AVRO):
        return _same, False
    if t in _INT_WIDEN_AVRO:
        return _to(_INT_WIDEN_AVRO[t]), False
    if pa.types.is_timestamp(t) and t.unit in ("ms", "us", "ns"):
        if t.tz is not None and t.tz not in _UTC:
            return None
        unit = "us" if t.unit == "ns" else t.unit
        cast = _to(pa.timestamp(unit, t.tz)) if t.unit == "ns" else _same
        return (_then(cast, _naive(unit)), True) if t.tz is not None else (cast, False)
    return None


def _orc_ok_nested(t: pa.DataType) -> bool:
    if pa.types.is_list(t) or pa.types.is_large_list(t):
        return _orc_ok_nested(t.value_type)
    if pa.types.is_struct(t):
        return all(_orc_ok_nested(t.field(i).type) for i in range(t.num_fields))
    return any(rule(t) for rule in _AS_IS_ORC)


def _orc_cast(t: pa.DataType) -> Cast | None:
    if pa.types.is_dictionary(t):
        inner = _orc_cast(t.value_type)
        return None if inner is None else _then(_decode, inner)
    if any(rule(t) for rule in _AS_IS_ORC):
        return _same
    if t in _INT_WIDEN_ORC:
        return _to(_INT_WIDEN_ORC[t])
    if pa.types.is_timestamp(t):
        return _in_ns_range(t.unit)
    if pa.types.is_nested(t) and _orc_ok_nested(t):
        return _same
    return None


def avro_plan(schema: pa.Schema, *, table: str) -> TargetPlan:
    names = list(schema.names)
    casts: list[Cast] = []
    utc: set[str] = set()
    for field in schema:
        if not avro_schema.valid_name(field.name) or names.count(field.name) > 1:
            raise TargetColumnTypeUnsupported(table=table, column=field.name, reason="avro_name")
        planned = _avro_cast(field.type)
        if planned is None:
            raise TargetColumnTypeUnsupported(
                table=table, column=field.name, arrow_type=str(field.type), format="avro"
            )
        casts.append(planned[0])
        if planned[1]:
            utc.add(field.name)
    return TargetPlan(schema, tuple(casts), frozenset(utc))


def orc_plan(schema: pa.Schema, *, table: str) -> TargetPlan:
    casts: list[Cast] = []
    for field in schema:
        cast = _orc_cast(field.type)
        if cast is None:
            raise TargetColumnTypeUnsupported(
                table=table, column=field.name, arrow_type=str(field.type), format="orc"
            )
        casts.append(cast)
    return TargetPlan(schema, tuple(casts), frozenset())


def prepare_chunk(chunk: pa.Table, plan: TargetPlan, *, table: str) -> pa.Table:
    """`chunk` cast to the write schema, then each column's lossless format cast."""
    failed = False
    cast: pa.Table | None = None
    try:
        cast = chunk.select(plan.schema.names).cast(plan.schema, safe=True)
    except (pa.ArrowInvalid, pa.ArrowNotImplementedError, KeyError, ValueError):
        failed = True
    if failed or cast is None:
        raise TargetSchemaMismatch(table=table, reason="chunk_does_not_cast_to_schema")
    columns: list[pa.ChunkedArray] = []
    bad: str | None = None
    for name, column_cast, column in zip(plan.schema.names, plan.casts, cast.columns, strict=True):
        try:
            columns.append(column_cast(column))
        except _Unwritable:
            bad = name
            break
    if bad is not None:
        raise TargetColumnTypeUnsupported(table=table, column=bad, reason="value_not_representable")
    return pa.Table.from_arrays(columns, names=plan.schema.names)


__all__ = ["TargetPlan", "avro_plan", "orc_plan", "prepare_chunk", "resolve_chunks"]

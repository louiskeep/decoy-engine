"""How a key column is brought to one comparable type before the key scan loads it into DuckDB.

Key ends match by the FK join's own key identity, never by an Arrow cast. Ends of one family
(every integer width, every string encoding, a dictionary of either, timestamps with a zone that
differ in unit or zone, durations that differ in unit) compare by value in DuckDB, which agrees
with the join. Any other mix maps each value to the join token (`decoy_engine.internal.
fk_tokens`) as VARCHAR, so the string "7" is not the integer 7 and a date is not a midnight
timestamp, as the join sees them. Nothing here carries a key value in an error or a log.

A zone-aware timestamp or a duration is kept out of the token path on purpose: the token for
those is an object repr that does not fold two units of one instant, and the only route that
accepts those types (the pandas join) matches them by value. They load as the int64 count of
their finest unit, as does a nanosecond time of day: DuckDB holds an interval, a zoned
timestamp and a time at microseconds, which would merge keys one nanosecond apart."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import pyarrow as pa
import pyarrow.compute as pc

from decoy_engine._arrow_types import is_text
from decoy_engine.internal.fk_tokens import NULL_FK_KEY, fk_join_key, fk_key_value

JOIN_KEY_KIND: Final = "join_token"
_UNITS: Final = ("s", "ms", "us", "ns")


def value_type(t: pa.DataType) -> pa.DataType:
    return t.value_type if pa.types.is_dictionary(t) else t


def _family(t: pa.DataType) -> tuple[str, pa.DataType | None]:
    """The comparison family of a key end. Any type not folded into one of the families above is
    its own family, so two different ones go through the join tokens."""
    v = value_type(t)
    if pa.types.is_integer(v):
        return ("int", None)
    if is_text(v):
        return ("text", None)
    if pa.types.is_timestamp(v) and v.tz is not None:
        return ("tz_timestamp", None)
    if pa.types.is_duration(v):
        return ("duration", None)
    return ("other", v)


def _common(family: str, typed: Sequence[pa.DataType]) -> str | pa.DataType:
    """The type one family loads as: an integer width, VARCHAR, or the finest unit of the ends."""
    first = value_type(typed[0])
    if family == "int":
        return "HUGEINT" if any(value_type(t) == pa.uint64() for t in typed) else "BIGINT"
    if family == "text":
        return pa.string()
    unit = max((value_type(t).unit for t in typed), key=_UNITS.index)
    if family == "tz_timestamp":
        return pa.timestamp(unit, tz=first.tz)
    if family == "duration":
        return pa.duration(unit)
    return first


def _as_count(t: pa.DataType) -> bool:
    """A type DuckDB would store coarser than Arrow, so its table holds the int64 count."""
    return (
        pa.types.is_duration(t)
        or (pa.types.is_timestamp(t) and t.tz is not None)
        or (pa.types.is_time64(t) and t.unit == "ns")
    )


def table_type(kind: pa.DataType) -> pa.DataType:
    """The column type a key table is created with for a cast `kind`."""
    return pa.int64() if _as_count(kind) else kind


def duck_types(schemas: Sequence[pa.Schema]) -> list[str | pa.DataType | None]:
    """How each key label is loaded, wide enough for every end. `None` keeps the declared type;
    `BIGINT` or `HUGEINT` is the integer width that holds every end; a type is the one every
    batch is cast to; `JOIN_KEY_KIND` maps each value to the join token as VARCHAR. The tables
    are created from these, never from the first batch, so a later wider batch cannot overflow or
    narrow. Null-typed ends are left out of the choice; a label that is null-typed everywhere
    loads as BIGINT, which holds only nulls."""
    out: list[str | pa.DataType | None] = []
    for n in range(len(schemas[0])):
        types = [s.types[n] for s in schemas]
        typed = [t for t in types if not pa.types.is_null(t)]
        if not typed:
            out.append("BIGINT")
        elif len({_family(t) for t in typed}) > 1:
            out.append(JOIN_KEY_KIND)
        elif all(t.equals(types[0]) for t in types) and not (
            pa.types.is_null(types[0]) or pa.types.is_dictionary(types[0]) or _as_count(types[0])
        ):
            out.append(None)
        else:
            out.append(_common(_family(typed[0])[0], typed))
    return out


def _token_array(array: pa.Array) -> pa.Array | None:
    """Each value as its FK join token (VARCHAR); a null stays SQL NULL so null counting is
    unchanged. `None` when the token functions refuse a value; their error text is dropped
    because it can quote the value."""
    try:
        tokens = [
            None if fk_key_value(v) is NULL_FK_KEY else fk_join_key(v) for v in array.to_pylist()
        ]
    except MemoryError:
        raise
    except Exception:
        return None
    return pa.array(tokens, type=pa.string())


def cast_batch(
    batch: pa.RecordBatch, duck: Sequence[str | pa.DataType | None]
) -> pa.RecordBatch | None:
    """The batch brought to its table's types (dictionaries decoded, join-key columns mapped to
    tokens), or `None` when a value has no token or no exact image in the common unit."""
    arrays = []
    for array, kind in zip(batch.columns, duck, strict=True):
        if isinstance(array, pa.DictionaryArray):
            array = array.dictionary_decode()
        if isinstance(kind, str) and kind == JOIN_KEY_KIND:
            array = _token_array(array)
            if array is None:
                return None
        elif isinstance(kind, pa.DataType) and not array.type.equals(kind):
            try:
                array = array.cast(kind)
            except pa.ArrowInvalid:
                return None
        if isinstance(kind, pa.DataType) and _as_count(kind):
            array = array.cast(pa.int64())
        if pa.types.is_floating(array.type):
            # The join reads a NaN key as null (`fk_key_value`); DuckDB would count it as a value.
            nan = pc.is_nan(array)  # type: ignore[attr-defined, unused-ignore]
            array = pc.if_else(nan, pa.scalar(None, array.type), array)  # type: ignore[attr-defined, unused-ignore]
        arrays.append(array)
    return pa.RecordBatch.from_arrays(arrays, names=batch.schema.names)

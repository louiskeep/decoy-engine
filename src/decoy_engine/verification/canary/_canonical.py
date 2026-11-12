"""Typed canonical text for Arrow cells, part of ``canary-span-normalization/v1`` (F34 E2).

Each supported Arrow type has one frozen text form, derived from the stored value and never from
pyarrow's Python-object conversion, whose result type depends on whether pandas is importable. A
cell is canonical text, null-like (never compared) or uncompared (counted, forces
``INCOMPLETE``). A schema with any type outside the table is unsupported before a cell is read;
``canonical_cells`` refuses such a column instead of falling back to text.

Pattern: ISO 8601 extended format (``YYYY-MM-DD``, ``HH:MM:SS``, proleptic Gregorian) over the
stored integer with floor division, as Arrow's own temporal kernels split days; Python float
``repr`` (shortest round trip, Gay's ``dtoa``); ``decimal`` ``format(d, "f")`` keeping scale;
strings read as bytes through Arrow's zero-copy binary view (the Arrow columnar spec defines
``utf8`` as ``binary`` with a UTF-8 guarantee) and decoded with the strict UTF-8 codec, so a
file's invalid UTF-8 is an uncompared cell, not a decoder exception.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from datetime import date

import pyarrow as pa

from decoy_engine.verification.canary._types import CanaryPolicyInputError


class CellState(enum.Enum):
    NULL_LIKE = "null_like"
    UNCOMPARED = "uncompared"


class SchemaSupport(enum.Enum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"


Cell = str | CellState
_Convert = Callable[[pa.Array], list[Cell]]

NULL, UNCOMPARED = CellState.NULL_LIKE, CellState.UNCOMPARED
DATE_MIN, DATE_MAX = -719_162, 2_932_896  # 0001-01-01 and 9999-12-31 as days since 1970-01-01
_EPOCH_ORDINAL = 719_163  # date(1970, 1, 1).toordinal()
_PER_SECOND = {"s": 1, "ms": 10**3, "us": 10**6, "ns": 10**9}
_DAY_SECONDS = 86_400
_DAY_MS = 86_400_000
_FLOAT_EXACT = 2**53


def _valid(arr: pa.Array) -> list[bool]:
    return arr.is_valid().to_numpy(zero_copy_only=False).tolist()


def _stored_values(arr: pa.Array) -> list:
    # The values buffer read with no validity bitmap; null slots are masked by ``_valid``. No
    # Python scalar is boxed (filling nulls would box one), so pyarrow's pandas shim stays out.
    dense = pa.Array.from_buffers(arr.type, len(arr), [None, arr.buffers()[1]], offset=arr.offset)
    return dense.to_numpy(zero_copy_only=False).tolist()


def _each(arr: pa.Array, render: Callable[[object], Cell]) -> list[Cell]:
    pairs = zip(_stored_values(arr), _valid(arr), strict=True)
    return [render(x) if ok else NULL for x, ok in pairs]


def _float_text(x: object) -> Cell:
    f = float(x)  # type: ignore[arg-type]
    if f != f or f in (float("inf"), float("-inf")):
        return NULL
    if f.is_integer() and abs(f) <= _FLOAT_EXACT:
        return f"{int(f):d}"
    return repr(f)


def _decimal(arr: pa.Array) -> list[Cell]:
    values = arr.to_numpy(zero_copy_only=False).tolist()
    return [NULL if d is None or not d.is_finite() else format(d, "f") for d in values]


def _date_text(days: int) -> str | None:
    if not DATE_MIN <= days <= DATE_MAX:
        return None
    d = date.fromordinal(days + _EPOCH_ORDINAL)
    return f"{d.year:04d}-{d.month:02d}-{d.day:02d}"


def _clock(seconds: int, frac: int, per_second: int) -> str:
    text = f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
    if frac:
        width = len(str(per_second)) - 1
        text += "." + f"{frac:0{width}d}".rstrip("0")
    return text


def _stored(arr: pa.Array, storage: pa.DataType) -> list[int | None]:
    ints = arr.cast(storage)
    return [n if ok else None for n, ok in zip(_stored_values(ints), _valid(ints), strict=True)]


def _date64_text(ms: int) -> Cell:
    return UNCOMPARED if ms % _DAY_MS else _date_text(ms // _DAY_MS) or UNCOMPARED


def _timestamp_text(v: int, per_second: int) -> Cell:
    per_day = _DAY_SECONDS * per_second
    day = _date_text(v // per_day)
    if day is None:
        return UNCOMPARED
    within = v % per_day
    return f"{day}T{_clock(within // per_second, within % per_second, per_second)}"


def _timestamp(arr: pa.Array) -> list[Cell]:
    per_second = _PER_SECOND[arr.type.unit]
    return _stored_cells(pa.int64(), lambda n: _timestamp_text(n, per_second))(arr)


def _time_text(v: int, per_second: int) -> Cell:
    if not 0 <= v < _DAY_SECONDS * per_second:
        return UNCOMPARED
    return _clock(v // per_second, v % per_second, per_second)


def _time(arr: pa.Array) -> list[Cell]:
    per_second = _PER_SECOND[arr.type.unit]
    storage = pa.int32() if pa.types.is_time32(arr.type) else pa.int64()
    return _stored_cells(storage, lambda n: _time_text(n, per_second))(arr)


# Zero-copy reinterpretation: same buffers, no UTF-8 validation on the way to bytes.
_BINARY_VIEW = {
    pa.string(): pa.binary(),
    pa.large_string(): pa.large_binary(),
    pa.string_view(): pa.binary_view(),
}


def _decode(raw: bytes | None) -> Cell:
    if raw is None:
        return NULL
    try:
        return raw.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return UNCOMPARED


def _utf8(arr: pa.Array) -> list[Cell]:
    view = _BINARY_VIEW.get(arr.type)
    raw = arr if view is None else arr.view(view)
    return [_decode(b) for b in raw.to_numpy(zero_copy_only=False).tolist()]


def _dictionary(arr: pa.Array) -> list[Cell]:
    # Each value decodes once; a cell referencing an invalid value (or no value) is uncompared.
    values = _utf8(arr.dictionary)
    out: list[Cell] = []
    for i, ok in zip(_stored_values(arr.indices), _valid(arr), strict=True):
        out.append(NULL if not ok else values[i] if 0 <= i < len(values) else UNCOMPARED)
    return out


def _stored_cells(storage: pa.DataType, render: Callable[[int], Cell]) -> _Convert:
    def convert(arr: pa.Array) -> list[Cell]:
        return [NULL if n is None else render(n) for n in _stored(arr, storage)]

    return convert


_SCALAR: tuple[tuple[Callable[[pa.DataType], bool], _Convert], ...] = (
    (pa.types.is_integer, lambda arr: _each(arr, lambda n: f"{n:d}")),
    (pa.types.is_boolean, lambda arr: _each(arr, lambda b: "true" if b else "false")),
    (pa.types.is_float32, lambda arr: _each(arr, _float_text)),
    (pa.types.is_float64, lambda arr: _each(arr, _float_text)),
    (pa.types.is_decimal128, _decimal),
    (pa.types.is_decimal256, _decimal),
    (pa.types.is_date32, _stored_cells(pa.int32(), lambda n: _date_text(n) or UNCOMPARED)),
    (pa.types.is_date64, _stored_cells(pa.int64(), _date64_text)),
    (pa.types.is_time32, _time),
    (pa.types.is_time64, _time),
    (pa.types.is_null, lambda arr: [NULL] * len(arr)),
    (pa.types.is_string, _utf8),
    (pa.types.is_large_string, _utf8),
    (pa.types.is_string_view, _utf8),
    (pa.types.is_binary, _utf8),
    (pa.types.is_large_binary, _utf8),
    (pa.types.is_fixed_size_binary, _utf8),
)


def _converter(typ: pa.DataType) -> _Convert | None:
    if isinstance(typ, pa.ExtensionType):
        return None
    if pa.types.is_dictionary(typ):
        return _dictionary if typ.value_type in _BINARY_VIEW else None
    if pa.types.is_timestamp(typ):
        return _timestamp if typ.tz in (None, "UTC") else None
    for matches, convert in _SCALAR:
        if matches(typ):
            return convert
    return None


def classify_schema(schema: pa.Schema) -> SchemaSupport:
    """Decide from the first batch's schema, before any cell is compared (named limit L7)."""
    if type(schema) is not pa.Schema:
        raise CanaryPolicyInputError
    if all(_converter(field.type) is not None for field in schema):
        return SchemaSupport.SUPPORTED
    return SchemaSupport.UNSUPPORTED


def canonical_cells(column: pa.Array) -> list[Cell]:
    """One canonical text, ``NULL_LIKE`` or ``UNCOMPARED`` per cell of a supported column."""
    convert = _converter(column.type) if isinstance(column, pa.Array) else None
    if convert is None:
        raise CanaryPolicyInputError
    return convert(column)

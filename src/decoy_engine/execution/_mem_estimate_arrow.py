"""Price a column by its Arrow type for the byte-estimate router.

`ColumnProfile.dtype` is a pandas label, and pandas reports many non-string Arrow
types (date, time, decimal, binary, tz timestamp, ...) as `object` or as a label the
cost table does not list. Treating "not a numpy fixed-width label" as "string"
sent those columns to the string sampler, which crashed. This module classifies
the Arrow type instead.

The goal is to bound memory, not to predict pandas. A classification may only
raise an estimate relative to a precise model, and a representation the module
cannot price in one simple step is `Unpriceable` (the router then goes bounded)
rather than guessed. Costs are the resident pandas form a full_frame run holds,
read off `_FIXED_WIDTH_DTYPE_BYTES`, not Arrow storage widths. Storage widths, for
a future disk-width caller: date32 4, date64 8, time32 4, time64 8, timestamp 8,
duration 8, decimal32/64/128/256 4/8/16/32.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pyarrow as pa

from decoy_engine.profile._readers import LazySource

_PLAIN_STRING_TYPES = (pa.string(), pa.large_string(), pa.binary(), pa.large_binary())


@dataclass(frozen=True)
class Fixed:
    """A fixed per-cell cost; `label` is a key of `_FIXED_WIDTH_DTYPE_BYTES`."""

    label: str


@dataclass(frozen=True)
class Declared:
    """Variable-width `object` column whose payload is known exactly from the type."""

    width_bytes: float


@dataclass(frozen=True)
class Sampled:
    """String or binary column priced from a measured average byte length.

    `decoded_type` is the plain type the sampler reads after at most one
    normalization step.
    """

    decoded_type: pa.DataType


@dataclass(frozen=True)
class Unpriceable:
    reason: str


ArrowSizeClass = Fixed | Declared | Sampled | Unpriceable


def is_wrapped_type(arrow_type: pa.DataType) -> bool:
    """Whether the top-level type is run-end-encoded, a dictionary or an extension.

    The outer `null_count` of such a column can hide logical nulls (a null run, a
    null in the dictionary values), so a resident caller prices it as nullable.
    """
    return (
        isinstance(arrow_type, pa.BaseExtensionType)
        or pa.types.is_dictionary(arrow_type)
        or pa.types.is_run_end_encoded(arrow_type)
    )


def resident_has_nulls(column: pa.Array | pa.ChunkedArray) -> bool:
    """`has_nulls` for a RESIDENT column: exact `null_count`, or True for a wrapper."""
    return True if is_wrapped_type(column.type) else column.null_count > 0


def _unwrap(arrow_type: pa.DataType) -> tuple[pa.DataType, list[str]]:
    """The innermost value type and the wrapper kinds stripped on the way."""
    wrappers: list[str] = []
    while True:
        if isinstance(arrow_type, pa.BaseExtensionType):
            wrappers.append("extension")
            arrow_type = arrow_type.storage_type
        elif pa.types.is_dictionary(arrow_type):
            wrappers.append("dictionary")
            arrow_type = arrow_type.value_type
        elif pa.types.is_run_end_encoded(arrow_type):
            wrappers.append("run_end_encoded")
            arrow_type = arrow_type.value_type
        else:
            return arrow_type, wrappers


def _is_string_family(t: pa.DataType) -> bool:
    return (
        pa.types.is_string(t)
        or pa.types.is_large_string(t)
        or pa.types.is_string_view(t)
        or pa.types.is_binary(t)
        or pa.types.is_large_binary(t)
        or pa.types.is_binary_view(t)
    )


def _classify_string_family(
    arrow_type: pa.DataType, base: pa.DataType, wrappers: list[str]
) -> ArrowSizeClass:
    """Sampled only when the sampler needs at most one normalization step.

    A dictionary never reaches here: `classify_column` rejects every dictionary first.
    """
    is_view = pa.types.is_string_view(base) or pa.types.is_binary_view(base)
    if not wrappers:
        if is_view:
            decoded = pa.large_string() if pa.types.is_string_view(base) else pa.large_binary()
            return Sampled(decoded)
        return Sampled(base)
    if wrappers == ["extension"] and not is_view:
        return Sampled(base)
    return Unpriceable(f"{arrow_type}: wrapped string representation, not sampled")


def _classify_numeric(base: pa.DataType, has_nulls: bool) -> ArrowSizeClass | None:
    if pa.types.is_boolean(base):
        return Fixed("pyobject[bool]" if has_nulls else "bool")
    if pa.types.is_integer(base):
        return Fixed("float64" if has_nulls else str(base))
    if pa.types.is_floating(base):
        return Fixed(f"float{base.bit_width}")
    if pa.types.is_timestamp(base):
        return Fixed("datetime64[ns]")
    if pa.types.is_duration(base):
        return Fixed("timedelta64[ns]")
    if pa.types.is_date(base):
        return Fixed("pyobject[date]")
    if pa.types.is_time(base):
        return Fixed("pyobject[time]")
    if pa.types.is_decimal(base):
        return Fixed("pyobject[decimal]")
    return None


def classify_column(arrow_type: pa.DataType, *, has_nulls: bool) -> ArrowSizeClass:
    """How to price a column of `arrow_type`.

    `has_nulls` decides the nullable form of int/uint below 64 bits and bool (pandas
    widens an int with nulls to float64 and holds a bool with nulls as objects). The
    caller supplies it from the column or the file footer, never from a profile
    sample. An extension or run-end-encoded wrapper is classified through its value
    type. Every dictionary is UNPRICEABLE: pandas decodes it to a Categorical whose
    cost depends on cardinality, and this module bounds memory rather than
    predicting pandas.
    """
    base, wrappers = _unwrap(arrow_type)
    if "dictionary" in wrappers:
        return Unpriceable(f"{arrow_type}: dictionary columns are not priced")
    if _is_string_family(base):
        return _classify_string_family(arrow_type, base, wrappers)
    if pa.types.is_null(base):
        return Declared(0.0)
    if pa.types.is_fixed_size_binary(base):
        return Declared(float(base.byte_width))
    priced = _classify_numeric(base, has_nulls)
    if priced is not None:
        return priced
    return Unpriceable(f"{arrow_type} has no resident-size model")


def _is_plain(t: pa.DataType) -> bool:
    return t in _PLAIN_STRING_TYPES


def normalize_for_sampling(column: pa.Array | pa.ChunkedArray) -> pa.Array | pa.ChunkedArray:
    """`column` as a plain string or binary array, applying exactly one step.

    One step is a view cast or an extension unwrap. Anything else, dictionaries
    included, raises `TypeError` naming the Arrow type before any Arrow kernel runs; the
    classifier never routes such a column here.
    """
    t = column.type
    if _is_plain(t):
        return column
    if pa.types.is_string_view(t) or pa.types.is_binary_view(t):
        return column.cast(pa.large_string() if pa.types.is_string_view(t) else pa.large_binary())
    if isinstance(t, pa.BaseExtensionType) and _is_plain(t.storage_type):
        if isinstance(column, pa.ChunkedArray):
            return pa.chunked_array([c.storage for c in column.chunks], type=t.storage_type)
        return column.storage
    raise TypeError(
        f"sample_average_string_bytes needs a plain string or binary column "
        f"(or one view cast or extension unwrap away), got {t}"
    )


_PANDAS_NULLABLE_DTYPES = frozenset(
    (
        *(f"Int{w}" for w in (8, 16, 32, 64)),
        *(f"UInt{w}" for w in (8, 16, 32, 64)),
        *(f"Float{w}" for w in (32, 64)),
        "boolean",
        "string",
    )
)


def pandas_nullable_columns(schema: pa.Schema) -> frozenset[str]:
    """Field names the schema's `b"pandas"` metadata marks as pandas nullable dtypes.

    A frame written from `Int8`, `boolean` and the like carries a validity mask even
    when no value is null, and pandas reads it back as the nullable extension dtype.
    Malformed metadata marks nothing.
    """
    raw = (schema.metadata or {}).get(b"pandas")
    if raw is None:
        return frozenset()
    try:
        columns = json.loads(raw)["columns"]
        return frozenset(
            str(c.get("field_name") or c["name"])
            for c in columns
            if c.get("numpy_type") in _PANDAS_NULLABLE_DTYPES
        )
    except (ValueError, KeyError, TypeError, AttributeError):
        return frozenset()


def column_arrow_types(
    resident: pa.Table | LazySource | None, profile_table: Any
) -> dict[str, tuple[pa.DataType, bool]]:
    """`{column: (arrow_type, has_nulls)}` for the profile's columns that a source can type.

    Nullability never comes from the profile: its `null_count` is a sample, and a
    sample misses nulls past its window. A resident column reports its exact
    `null_count` (True for a wrapped type). A lazy column reads the Parquet footer:
    complete statistics give an exact count, otherwise the field's `nullable` flag
    stands, and a positive profile count can only widen. On both, a column the
    schema's pandas metadata marks as a nullable extension dtype is nullable. No
    source gives `{}`.
    """
    if isinstance(resident, pa.Table):
        present = set(resident.column_names)
        nullable = pandas_nullable_columns(resident.schema)
        return {
            c.name: (
                resident.schema.field(c.name).type,
                c.name in nullable or resident_has_nulls(resident.column(c.name)),
            )
            for c in profile_table.columns
            if c.name in present
        }
    if isinstance(resident, LazySource):
        schema = resident.schema
        counts = resident.column_null_counts()
        nullable = pandas_nullable_columns(schema)
        types: dict[str, tuple[pa.DataType, bool]] = {}
        for c in profile_table.columns:
            if c.name not in schema.names:
                continue
            field = schema.field(c.name)
            exact = counts.get(c.name)
            has_nulls = field.nullable if exact is None else exact > 0
            types[c.name] = (
                field.type,
                has_nulls or c.name in nullable or getattr(c, "null_count", 0) > 0,
            )
        return types
    return {}


# No Arrow type: widen the labels whose representation can carry a null mask.
_NULLABLE_LABEL_CLASS: Mapping[str, str] = {
    **dict.fromkeys(
        (
            *("int8", "int16", "int32", "uint8", "uint16", "uint32"),
            *("Int8", "Int16", "Int32", "UInt8", "UInt16", "UInt32"),
        ),
        "float64",
    ),
    "bool": "pyobject[bool]",
    "boolean": "pyobject[bool]",
}


def widen_label_without_arrow_type(label: str) -> str:
    """The cost label for a profile `label` when no Arrow type is available."""
    return _NULLABLE_LABEL_CLASS.get(label, label)

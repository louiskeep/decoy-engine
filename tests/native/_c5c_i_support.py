"""Shared builders for the positional-Faker-over-numeric-sources tests (chunked and unified).

The admitted families are the integer, unsigned integer, boolean and floating-point Arrow types.
Metadata forms are built by hand so an Arrow-valid NaN can sit under each pandas dtype the
oracle might rebuild.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa

SIGNED = [pa.int8(), pa.int16(), pa.int32(), pa.int64()]
UNSIGNED = [pa.uint8(), pa.uint16(), pa.uint32(), pa.uint64()]
FLOATS = [pa.float32(), pa.float64()]
ADMITTED = [*SIGNED, *UNSIGNED, pa.bool_(), *FLOATS]

NULL_AT = (1, 4, 7)


def type_id(typ: pa.DataType) -> str:
    return str(typ)


def boundary_values(typ: pa.DataType, n: int = 12) -> list[Any]:
    """`n` non-null values that include the type's own extremes."""
    if pa.types.is_boolean(typ):
        return [bool(i % 3 == 0) for i in range(n)]
    if pa.types.is_integer(typ):
        info = np.iinfo(typ.to_pandas_dtype())
        head = [int(info.min), int(info.max), 0 if info.min < 0 else 1, int(info.min) + 1]
        head += [int(info.max) - 1]
        return (head + list(range(5, 5 + n)))[:n]
    base = [0.0, -0.0, math.inf, -math.inf, 1.5, 3.0e38, -2.25]
    return (base + [float(i) for i in range(n)])[:n]


def typed_array(typ: pa.DataType, *, nulls: bool, n: int = 12) -> pa.Array:
    values = boundary_values(typ, n)
    if nulls:
        values = [None if i in NULL_AT else v for i, v in enumerate(values)]
    return pa.array(values, type=typ)


def with_nan(typ: pa.DataType, n: int = 12) -> pa.Array:
    """A float column holding real NaN values, nulls and infinities, all Arrow-valid or null."""
    values: list[Any] = boundary_values(typ, n)
    for i in (2, 5):
        values[i] = math.nan
    values[7] = None
    return pa.array(values, type=typ)


def numpy_meta(typ: pa.DataType) -> dict[bytes, bytes]:
    frame = pd.DataFrame({"f": np.array([1.0, 2.0], dtype=typ.to_pandas_dtype())})
    return dict(pa.Table.from_pandas(frame, preserve_index=False).schema.metadata)


def nullable_meta(typ: pa.DataType) -> dict[bytes, bytes]:
    name = "Float32" if typ == pa.float32() else "Float64"
    frame = pd.DataFrame({"f": pd.array([1.0, None], dtype=name)})
    return dict(pa.Table.from_pandas(frame, preserve_index=False).schema.metadata)


def arrow_ext_meta(typ: pa.DataType) -> dict[bytes, bytes]:
    frame = pd.DataFrame({"f": pd.array([1.0, None], dtype=pd.ArrowDtype(typ))})
    return dict(pa.Table.from_pandas(frame, preserve_index=False).schema.metadata)


METADATA_FORMS = {
    "numpy": numpy_meta,
    "nullable": nullable_meta,
    "arrow_ext": arrow_ext_meta,
}


def float_table(
    typ: pa.DataType, form: str, array: pa.Array | None = None, *, with_p: bool = True
) -> pa.Table:
    """A float Faker source `f` under one pandas metadata form, beside an integer `p`."""
    array = with_nan(typ) if array is None else array
    cols: dict[str, pa.Array] = {"f": array}
    if with_p:
        cols["p"] = pa.array(list(range(len(array))), pa.int64())
    table = pa.table(cols)
    return table.replace_schema_metadata(METADATA_FORMS[form](typ))


def int_table(typ: pa.DataType, array: pa.Array) -> pa.Table:
    return pa.table({"f": array, "p": pa.array(list(range(len(array))), pa.int64())})

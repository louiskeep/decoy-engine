"""Numeric dtype restoration for generated columns.

The sequence generator renders every value through a prefix/suffix template and the faker
provider layer str-coerces every return (it is shared with masking, which needs text cells).
Left alone, a plain counter or a ``pyfloat`` column reaches ``derived`` arithmetic, ordered
``grouped_series`` and the Arrow table as text. This module is the single place that puts the
declared numeric type back, so each numeric generator keeps its dtype end to end. Rendered text
is unchanged: ``str(int)``/``str(float)`` give back the exact cell text the coercion produced.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from typing import Any

import pyarrow as pa

_NUMERIC_ANNOTATIONS: dict[Any, Callable[[str], Any]] = {
    int: int,
    float: float,
    "int": int,
    "float": float,
}
_ARROW_TYPES: dict[Callable[[str], Any], pa.DataType] = {int: pa.int64(), float: pa.float64()}


def _faker_numeric_cast(
    faker_type: str,
    probe: Any,
    custom: Mapping[str, Any],
) -> Callable[[str], Any] | None:
    """The cast for a stock Faker provider declared to return int or float, else None.

    Custom providers override stock names and carry no declared return type, so they stay text.
    """
    if faker_type in custom:
        return None
    method = getattr(probe, faker_type, None)
    if not callable(method):
        return None
    try:
        annotation = inspect.signature(method).return_annotation
    except (TypeError, ValueError):
        return None
    try:
        return _NUMERIC_ANNOTATIONS.get(annotation)
    except TypeError:
        return None


def sequence_is_plain(col: Mapping[str, Any]) -> bool:
    return not col.get("prefix") and not col.get("suffix") and int(col.get("pad_length", 0)) <= 0


def numeric_cast(
    col: Mapping[str, Any],
    *,
    faker_probe: Any = None,
    custom_providers: Mapping[str, Any] | None = None,
) -> Callable[[str], Any] | None:
    """The cast for a column whose generator declares a numeric output, else None.

    Only an unformatted ``sequence`` and a stock ``faker`` provider annotated ``int`` or ``float``
    qualify. Text that merely looks numeric (zip codes, padded ids) is never inferred from values.
    """
    kind = col.get("type")
    if kind == "sequence" and sequence_is_plain(col):
        return int
    if kind == "faker" and faker_probe is not None:
        return _faker_numeric_cast(
            str(col.get("faker_type", "word")), faker_probe, custom_providers or {}
        )
    return None


def numeric_arrow_type(
    col: Mapping[str, Any],
    *,
    faker_probe: Any = None,
    custom_providers: Mapping[str, Any] | None = None,
) -> pa.DataType | None:
    """Arrow type matching :func:`numeric_cast`, so the declared schema and the values agree."""
    cast = numeric_cast(col, faker_probe=faker_probe, custom_providers=custom_providers)
    return None if cast is None else _ARROW_TYPES[cast]


def restore_numeric_dtype(
    col: Mapping[str, Any],
    values: list[Any],
    *,
    faker_probe: Any = None,
    custom_providers: Mapping[str, Any] | None = None,
) -> list[Any]:
    """Return *values* with the numeric type the column's generator declares."""
    cast = numeric_cast(col, faker_probe=faker_probe, custom_providers=custom_providers)
    if cast is None:
        return values
    out: list[Any] = []
    for v in values:
        if isinstance(v, str):
            try:
                v = cast(v)
            except ValueError:
                pass
        out.append(v)
    return out


def generated_table_to_pandas(table: pa.Table) -> Any:
    """Convert a generated table to pandas without widening nullable ints to float64.

    ``Table.to_pandas()`` turns an int64 column that holds any null into float64, which prints
    ``1.0`` where the cell text was ``1``. Every integer column goes through pandas' nullable
    integer dtypes instead, so the text written from the frame matches the generated values.
    """
    from decoy_engine.execution._fk_keys import to_pandas_fk_safe

    ints = {f.name for f in table.schema if pa.types.is_integer(f.type)}
    return to_pandas_fk_safe(table, ints)

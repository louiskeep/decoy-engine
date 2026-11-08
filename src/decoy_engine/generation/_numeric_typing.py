"""Plan-time numeric typing for generated columns.

The sequence generator renders every value through a prefix/suffix template and the faker
provider layer str-coerces every return (it is shared with masking, which needs text cells).
Left alone, a plain counter or a ``pyfloat`` column reaches ``derived`` arithmetic, ordered
``grouped_series`` and the Arrow table as text. :func:`numeric_plan` decides once, from the
configuration, which columns are numeric; the admission schema and every emitter read that one
answer. A column is numeric only when it is provably safe:

* the values fit int64 (a sequence's first and last value; a faker int provider's declared
  bounds), so a numeric column can never overflow where the text column would not;
* the column is not the parent key of a relationship with a child that is not generated, because
  a masked or source child reads the key as text and a typed parent would stop matching it.

Anything else stays text exactly as before. Rendered text is unchanged for numeric columns:
``str(int)`` and ``str(float)`` give back the cell text the coercion produced.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import Any

import pyarrow as pa

_INT64_MIN, _INT64_MAX = -(2**63), 2**63 - 1
# Stock Faker providers with a bounded integer result; kwarg names and defaults per provider.
# Anything not listed here stays text, because its range cannot be proven from the config.
_INT_PROVIDERS: dict[str, tuple[str, str, int, int]] = {
    "pyint": ("min_value", "max_value", 0, 9999),
    "random_int": ("min", "max", 0, 9999),
    "random_digit": ("", "", 0, 9),
    "random_digit_not_null": ("", "", 1, 9),
}
_MAX_RANDOM_NUMBER_DIGITS = 18  # 10**18 - 1 is the largest 18-digit value, inside int64


def sequence_is_plain(col: Mapping[str, Any]) -> bool:
    return not col.get("prefix") and not col.get("suffix") and int(col.get("pad_length", 0)) <= 0


def _fits(*values: int) -> bool:
    return all(_INT64_MIN <= v <= _INT64_MAX for v in values)


def _sequence_type(col: Mapping[str, Any], rows: int) -> pa.DataType | None:
    if not sequence_is_plain(col):
        return None
    start, step = int(col.get("start", 1)), int(col.get("step", 1))
    return pa.int64() if _fits(start, start + max(rows - 1, 0) * step) else None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _faker_type(
    col: Mapping[str, Any], probe: Any, custom: Mapping[str, Any]
) -> pa.DataType | None:
    name = str(col.get("faker_type", "word"))
    if name in custom:
        return None
    kwargs = col.get("faker_kwargs")
    kwargs = kwargs if isinstance(kwargs, dict) else {}
    if name in _INT_PROVIDERS:
        lo_key, hi_key, lo, hi = _INT_PROVIDERS[name]
        lo, hi = kwargs.get(lo_key, lo), kwargs.get(hi_key, hi)
        return pa.int64() if _is_int(lo) and _is_int(hi) and _fits(lo, hi) else None
    if name == "random_number":
        digits = kwargs.get("digits")
        ok = digits is None or (_is_int(digits) and 0 <= digits <= _MAX_RANDOM_NUMBER_DIGITS)
        return pa.int64() if ok else None
    method = getattr(probe, name, None)
    if not callable(method):
        return None
    try:
        annotation = inspect.signature(method).return_annotation
    except (TypeError, ValueError):
        return None
    return pa.float64() if annotation is float else None


def _text_key_parents(config: Mapping[str, Any], generated: set[str]) -> set[tuple[str, str]]:
    """Parent columns of relationships that have a child table that is not generated."""
    out: set[tuple[str, str]] = set()
    for rel in config.get("relationships") or []:
        children = rel.get("children") or []
        if all(c.get("table") in generated for c in children):
            continue
        parent = rel.get("parent") or {}
        for c in parent.get("columns") or []:
            out.add((str(parent.get("table")), str(c)))
    return out


def numeric_plan(
    config: Mapping[str, Any],
    *,
    provider_snapshot: Mapping[str, Any] | None = None,
) -> dict[tuple[str, str], pa.DataType]:
    """``{(table, column): int64 | float64}`` for each generated column that is numeric."""
    from decoy_engine.generation.synthesize import _get_default_faker
    from decoy_engine.internal.faker_setup import snapshot_custom_faker_providers

    tables = [t for t in config.get("tables") or [] if t.get("generate_columns")]
    probe = custom = None
    out: dict[tuple[str, str], pa.DataType] = {}
    held_as_text: set[tuple[str, str]] | None = None
    for t in tables:
        for col in t["generate_columns"]:
            kind = col.get("type")
            if kind == "sequence":
                dtype = _sequence_type(col, int(t.get("row_count") or 0))
            elif kind == "faker":
                if probe is None:
                    probe = _get_default_faker()
                    custom = (
                        provider_snapshot
                        if provider_snapshot is not None
                        else snapshot_custom_faker_providers()
                    )
                dtype = _faker_type(col, probe, custom or {})
            else:
                continue
            if dtype is None:
                continue
            if held_as_text is None:
                held_as_text = _text_key_parents(config, {t["name"] for t in tables})
            if (t["name"], col["name"]) not in held_as_text:
                out[(t["name"], col["name"])] = dtype
    return out


def restore_declared(
    col: Mapping[str, Any], values: list[Any], dtype: pa.DataType | None
) -> list[Any]:
    """Return *values* typed as the plan declared; text cells of a numeric column are parsed."""
    if dtype is None or col.get("type") not in ("sequence", "faker"):
        return values
    if pa.types.is_integer(dtype):
        cast: Any = int
    elif pa.types.is_floating(dtype):
        cast = float
    else:
        return values
    return [cast(v) if isinstance(v, str) else v for v in values]


def generated_table_to_pandas(table: pa.Table) -> Any:
    """Convert a generated table to pandas without widening nullable ints to float64.

    ``Table.to_pandas()`` turns an int64 column that holds any null into float64, which prints
    ``1.0`` where the cell text was ``1``. Every integer column goes through pandas' nullable
    integer dtypes instead, so the text written from the frame matches the generated values.
    """
    from decoy_engine.execution._fk_keys import to_pandas_fk_safe

    ints = {f.name for f in table.schema if pa.types.is_integer(f.type)}
    return to_pandas_fk_safe(table, ints)

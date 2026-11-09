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
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pyarrow as pa

from decoy_engine.generation._numeric_derived import derived_uses_as_numbers

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


@dataclass(frozen=True)
class Reader:
    """How one kind of sibling-value reader treats a numeric column."""

    safe: bool
    why: str


# The closed registry of every way a generated column's values are read by another column. A
# numeric column must be safe for ALL of its readers, so a reader that is not listed here, or a
# column type that is not listed in READER_KEYS, keeps the column text. Add a kind here only after
# checking it against typed input; tests/sentry/test_numeric_reader_registry.py fails otherwise.
READERS: dict[str, Reader] = {
    "formula.references": Reader(
        False, "formula locals are the cell text; str methods and + need text"
    ),
    "derived.expression": Reader(
        True, "decided per expression by derived_uses_as_numbers, not per kind"
    ),
    "derived_aggregate.column": Reader(
        False, "aggregate input coercion is not verified for typed input"
    ),
    "grouped_series.order_by": Reader(True, "orders by value, so numeric order is the correct one"),
    "grouped_series.group_by": Reader(False, "the group label is hashed from its text"),
    "group_key.group_by": Reader(False, "the group label is hashed from its text"),
    "windowed_date.anchor": Reader(False, "the anchor must be date text"),
    "statistical.condition_on": Reader(False, "matched against snapshot category labels as text"),
    "reference.reference_column": Reader(
        True, "copies the parent cell and its type; the group is checked together"
    ),
    "relationship": Reader(
        True, "safe only when every paired column is generated with the same planned type"
    ),
}
# column type -> (config key, registry id) for each sibling it reads. `expression` is scanned for
# identifiers. Types that read no sibling map to an empty tuple.
READER_KEYS: dict[str, tuple[tuple[str, str], ...]] = {
    "faker": (),
    "sequence": (),
    "categorical": (),
    "formula": (("references", "formula.references"),),
    "reference": (),
    "statistical": (("condition_on", "statistical.condition_on"),),
    "derived": (("expression", "derived.expression"),),
    "derived_aggregate": (("column", "derived_aggregate.column"),),
    "grouped_series": (
        ("order_by", "grouped_series.order_by"),
        ("group_by", "grouped_series.group_by"),
    ),
    "windowed_date": (("anchor", "windowed_date.anchor"),),
    "group_key": (("group_by", "group_key.group_by"),),
}
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
Key = tuple[str, str]


def _names_read(col: Mapping[str, Any], key: str) -> set[str]:
    value = col.get(key)
    if key == "expression":
        return set(_IDENTIFIER.findall(value)) if isinstance(value, str) else set()
    return {v for v in (value if isinstance(value, list) else [value]) if isinstance(v, str)}


def _reference_root(tables: Mapping[str, Any], key: Key) -> Key:
    """Follow ``reference`` columns to the column that decides the type (cycles stop at the loop)."""
    seen: set[Key] = set()
    while key not in seen:
        seen.add(key)
        col = next(
            (c for c in tables.get(key[0], {}).get("generate_columns", []) if c["name"] == key[1]),
            None,
        )
        if col is None or col.get("type") != "reference":
            return key
        key = (str(col.get("reference_table")), str(col.get("reference_column")))
    return key


def numeric_plan(
    config: Mapping[str, Any],
    *,
    provider_snapshot: Mapping[str, Any] | None = None,
) -> dict[Key, pa.DataType]:
    """``{(table, column): int64 | float64}`` for each generated column that is numeric.

    A column starts as a candidate when its values provably fit (see the module docstring) and
    then stays numeric only while every reader of it, and of every ``reference`` column that copies
    it, is numeric-safe. Removing a column can make another reader unsafe, so this runs to a fixed
    point; it only ever removes, so it terminates.
    """
    from decoy_engine.generation._admission import _categorical_type
    from decoy_engine.generation.synthesize import _get_default_faker
    from decoy_engine.internal.faker_setup import snapshot_custom_faker_providers

    tables = {t["name"]: t for t in config.get("tables") or [] if t.get("generate_columns")}
    cols: dict[Key, Mapping[str, Any]] = {
        (tn, c["name"]): c for tn, t in tables.items() for c in t["generate_columns"]
    }
    probe = custom = None
    base: dict[Key, pa.DataType] = {}
    for (tn, cn), col in cols.items():
        kind = col.get("type")
        if kind == "sequence":
            dtype = _sequence_type(col, int(tables[tn].get("row_count") or 0))
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
        if dtype is not None:
            base[(tn, cn)] = dtype
    if not base:
        return {}

    members: dict[Key, list[Key]] = {}
    for key in cols:
        members.setdefault(_reference_root(tables, key), []).append(key)
    pairs: list[list[tuple[Key, Key]]] = []  # one entry per relationship column pair
    for rel in config.get("relationships") or []:
        parent = rel.get("parent") or {}
        for i, pcol in enumerate(parent.get("columns") or []):
            row = [
                (
                    (str(parent.get("table")), str(pcol)),
                    (str(c.get("table")), str((c.get("columns") or [])[i])),
                )
                for c in rel.get("children") or []
                if i < len(c.get("columns") or [])
            ]
            pairs.append(row)
    in_pairs = {end for row in pairs for p, c in row for end in (p, c)}

    candidates = set(base)

    def planned(key: Key) -> pa.DataType | None:
        root = _reference_root(tables, key)
        if root in candidates:
            return base[root]
        col = cols.get(root)
        if col is None:
            return None
        kind = col.get("type")
        if kind in ("sequence", "faker"):
            return pa.string()
        if kind == "categorical":
            return _categorical_type(dict(col))[0]
        return None

    def is_number(key: Key) -> bool:
        dtype = planned(key) if key in cols else None
        return dtype is not None and (pa.types.is_integer(dtype) or pa.types.is_floating(dtype))

    def reader_ok(m: Key) -> bool:
        tn, cn = m
        for other in tables[tn]["generate_columns"]:
            if other["name"] == cn:
                continue
            spec = READER_KEYS.get(str(other.get("type")))
            if spec is None:
                return False  # unknown column type: it may read anything
            for key, rid in spec:
                if cn not in _names_read(other, key):
                    continue
                if rid == "derived.expression":
                    if not derived_uses_as_numbers(
                        str(other.get(key)), cn, lambda name: is_number((tn, name))
                    ):
                        return False
                elif not READERS[rid].safe:
                    return False
        if m in in_pairs:
            for row in pairs:
                if any(m in (p, c) for p, c in row) and not _pairs_agree(row):
                    return False
        return True

    def _pairs_agree(row: list[tuple[Key, Key]]) -> bool:
        for p, c in row:
            pt, ct = planned(p), planned(c)  # None for an end that is not generated
            if pt is None or pt != ct:
                return False
        return True

    changed = True
    while changed:
        changed = False
        for root in sorted(candidates):
            if not all(reader_ok(m) for m in members.get(root, [root])):
                candidates.discard(root)
                changed = True
    return {k: base[k] for k in candidates}


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

"""Value checks for the `constant` and `distribution` generate types and the `output_type` option.

Callables for the option registry (`_generate_options`): each takes the submitted value and the
column's flat options (plus its `type`) and returns ``(relative location, reason code)`` pairs.
Nothing here quotes a submitted value. The family a `distribution` column chose is read from the
options; the table of which keys a family takes lives in
`_generate_options.CONDITIONAL_REQUIRED`.

The declared value type of a `categorical`, `constant` or `distribution` column, and the text
type of a `sequence` or `faker` column a reader keeps as text, live here too: numeric typing
(`generation/_numeric_typing`) and admission build on them, and validation reads the same
answer for a per-parent aggregate's input before any data exists.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from typing import Any

import pyarrow as pa

from decoy_engine import reasons
from decoy_engine.errors import PipelineValidationError

__all__ = [
    "OUTPUT_TYPE_MAX_SCALE",
    "categorical_arrow_type",
    "clip_bound",
    "constant_arrow_type",
    "constant_value",
    "finite",
    "generated_arrow_type",
    "high_bound",
    "numeric_as_text_read",
    "output_type_value",
    "parametric_arrow_type",
    "parse_output_type",
    "refuse_fixed_width_output_types",
]

Problem = tuple[tuple[str | int, ...], str]

_TYPE = str(reasons.config.GENERATE_OPTION_TYPE)
_RANGE = str(reasons.config.GENERATE_OPTION_RANGE)

_VALUE = str(reasons.config.GENERATE_OPTION_VALUE_UNKNOWN)
_UNSUPPORTED = str(reasons.config.GENERATE_OUTPUT_TYPE_UNSUPPORTED)

_INT64_MIN, _INT64_MAX = -(2**63), 2**63 - 1

_OK: list[Problem] = []


def _as_float(value: Any) -> float | None:
    """The value as a finite float, or None when it is not one exactly.

    An int that float64 cannot hold exactly (past 2**53) is refused, since the draw would round
    it and could land outside the bounds the config set.
    """
    try:
        number = float(value)
    except OverflowError:
        return None
    if not math.isfinite(number) or (type(value) is int and number != value):
        return None
    return number


def _is_number(value: Any) -> bool:
    return type(value) in (int, float)


def finite(value: Any, _: Mapping[str, Any]) -> list[Problem]:
    """A finite int or float; a bool is not a number."""
    if not _is_number(value):
        return [((), _TYPE)]
    return _OK if _as_float(value) is not None else [((), _RANGE)]


def high_bound(value: Any, extras: Mapping[str, Any]) -> list[Problem]:
    """`high` of a uniform column: finite and above `low` once `low` is itself a number."""
    if problems := finite(value, extras):
        return problems
    low = _as_float(extras.get("low")) if _is_number(extras.get("low")) else None
    if low is not None:
        if float(value) <= low:
            return [((), _RANGE)]
        # A width that overflows to infinity would put inf in the column.
        if not math.isfinite(float(value) - low):
            return [((), _RANGE)]
    return _OK


def clip_bound(which: str) -> Callable[[Any, Mapping[str, Any]], list[Problem]]:
    """Check for `min` or `max`: a finite number; and
    `min <= max` once both are numbers (reported on `max`)."""

    def check(value: Any, extras: Mapping[str, Any]) -> list[Problem]:
        if problems := finite(value, extras):
            return problems
        other = extras.get("min")
        if which == "max" and _is_number(other):
            low = _as_float(other)
            if low is not None and low > float(value):
                return [((), _RANGE)]
        return _OK

    return check


def constant_value(value: Any, _: Mapping[str, Any]) -> list[Problem]:
    """The one value a `constant` column repeats: a str, a bool, an int64 or a finite float."""
    if isinstance(value, str) or type(value) is bool:
        return _OK
    if type(value) is int:
        return _OK if _INT64_MIN <= value <= _INT64_MAX else [((), _RANGE)]
    if type(value) is float:
        return _OK if math.isfinite(value) else [((), _RANGE)]
    return [((), _TYPE)]


# -- output_type ------------------------------------------------------------------------------

OUTPUT_TYPE_MAX_SCALE = 18
_OUTPUT_TYPE = re.compile(r"(int|date|bool)|decimal\(([0-9]{1,3})\)", re.ASCII)

# Which output types each generate type can carry. A type missing here does not take the option.
_ALL = ("int", "decimal", "date", "bool")
_NUMBER = ("int", "decimal")
OUTPUT_TYPE_KINDS: Mapping[str, tuple[str, ...]] = {
    "constant": _ALL,
    "lookup": _ALL,
    "categorical": _ALL,
    "derived": _ALL,
    "formula": _ALL,
    "faker": _ALL,
    "distribution": _NUMBER,
    "sequence": _NUMBER,
    "grouped_series": _NUMBER,
    "derived_aggregate": _NUMBER,
    "windowed_date": ("date",),
}
# A sequence prints text around its number; a number type would drop or refuse that text.
_SEQUENCE_TEXT_KEYS = ("prefix", "suffix", "pad_length")


def parse_output_type(text: Any) -> tuple[str, int | None] | None:
    """``(kind, scale)`` for ``int``, ``decimal(n)``, ``date`` or ``bool``, else None.

    ``scale`` is None except for ``decimal``. The scale's range is `output_type_value`'s rule, so a
    caller that only needs the shape does not repeat it.
    """
    match = _OUTPUT_TYPE.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        return None
    if match.group(1):
        return match.group(1), None
    return "decimal", int(match.group(2))


def output_type_value(value: Any, extras: Mapping[str, Any]) -> list[Problem]:
    """The `output_type` option: parseable, in range, and carried by this column type."""
    if not isinstance(value, str):
        return [((), _TYPE)]
    parsed = parse_output_type(value)
    if parsed is None:
        return [((), _VALUE)]
    kind, scale = parsed
    if scale is not None and scale > OUTPUT_TYPE_MAX_SCALE:
        return [((), _RANGE)]
    allowed = OUTPUT_TYPE_KINDS.get(str(extras.get("type")), ())
    if kind not in allowed:
        return [((), _UNSUPPORTED)]
    if extras.get("type") == "sequence" and any(k in extras for k in _SEQUENCE_TEXT_KEYS):
        return [((), _UNSUPPORTED)]
    return _OK


# -- the declared value type numeric typing starts from -------------------------------------------


def categorical_arrow_type(col: Mapping[str, Any]) -> tuple[pa.DataType | None, str]:
    """The Arrow type of homogeneous scalar categories, or None and why not."""
    cats = col.get("categories", ["Category A", "Category B"])
    if not isinstance(cats, list) or not cats:
        return None, "categories_not_a_nonempty_list"
    weights = col.get("weights")
    if weights is not None and (not isinstance(weights, list) or len(weights) != len(cats)):
        return None, "weights_do_not_match_categories"
    kinds = {type(c) for c in cats}
    if kinds == {str}:
        return pa.string(), "eligible"
    if kinds == {bool}:
        return pa.bool_(), "eligible"
    if kinds == {int}:
        if all(_INT64_MIN <= c <= _INT64_MAX for c in cats):
            return pa.int64(), "eligible"
        return None, "int_category_outside_int64"
    if kinds == {float}:
        return pa.float64(), "eligible"
    return None, "mixed_or_unsupported_category_types"


def constant_arrow_type(col: Mapping[str, Any]) -> pa.DataType:
    """Arrow type of one homogeneous value, by the rule `categorical_arrow_type` uses.

    A bool is tested before an int because `bool` is an `int` subclass.
    """
    value = col.get("value")
    if isinstance(value, bool):
        return pa.bool_()
    if isinstance(value, int):
        return pa.int64()
    if isinstance(value, float):
        return pa.float64()
    return pa.string()


def parametric_arrow_type(col: Mapping[str, Any]) -> pa.DataType:
    return pa.float64()


def generated_arrow_type(col: Mapping[str, Any]) -> pa.DataType | None:
    """The type of the values a generator makes for a column numeric typing keeps as text.

    `sequence` and `faker` render text; numeric typing types them as numbers only while every
    reader is numeric-safe. None for a type whose values the config does not fix.
    """
    kind = col.get("type")
    if kind in ("sequence", "faker"):
        return pa.string()
    if kind == "categorical":
        return categorical_arrow_type(col)[0]
    if kind == "constant":
        return constant_arrow_type(col)
    if kind == "distribution":
        return parametric_arrow_type(col)
    return None


def numeric_as_text_read(col: Mapping[str, Any]) -> bool:
    """Whether a column read by a numeric-unsafe reader still holds numbers.

    Such a reader keeps the column off numeric typing's candidates, so its emitted type is the
    declared `output_type` (a number kind is ``int`` or ``decimal(n)``), else
    `generated_arrow_type`.
    """
    parsed = parse_output_type(col.get("output_type"))
    if parsed is not None:
        return parsed[0] in _NUMBER
    dtype = generated_arrow_type(col)
    return dtype is not None and (pa.types.is_integer(dtype) or pa.types.is_floating(dtype))


# Layout types the fixed-width writer prints; a decimal, a date or a bool cell it refuses.
_FIXED_WIDTH_REFUSED = ("decimal", "date", "bool")


def refuse_fixed_width_output_types(config: Mapping[str, Any]) -> None:
    """Raise `PipelineValidationError` for a fixed-width target column the writer cannot print.

    `profile._fixed_width_writer` takes `str`, `int` and `float` cells only, so a generate column
    with `output_type` ``decimal(n)``, ``date`` or ``bool`` that the target's layout carries would
    fail at write time, after the whole run. The location is the target layout column. The message
    is the registry summary; nothing from the config is quoted and nothing is chained.
    """
    targets = config.get("targets")
    tables = config.get("tables")
    if not isinstance(targets, Mapping) or not isinstance(tables, list):
        return
    for table in tables:
        if not isinstance(table, Mapping):
            continue
        target = targets.get(table.get("name"))
        if not isinstance(target, Mapping) or target.get("format") != "fixed_width":
            continue
        layout = target.get("layout")
        columns = layout.get("columns") if isinstance(layout, Mapping) else None
        in_layout = {c.get("name") for c in columns or [] if isinstance(c, Mapping)}
        for col in table.get("generate_columns") or []:
            if not isinstance(col, Mapping) or col.get("name") not in in_layout:
                continue
            parsed = parse_output_type(col.get("output_type"))
            if parsed is not None and parsed[0] in _FIXED_WIDTH_REFUSED:
                raise PipelineValidationError(
                    reasons.lookup(_UNSUPPORTED)[0].summary,
                    path=f"targets.{table.get('name')}.layout.columns.{col.get('name')}",
                    code=reasons.config.GENERATE_OUTPUT_TYPE_UNSUPPORTED,
                )

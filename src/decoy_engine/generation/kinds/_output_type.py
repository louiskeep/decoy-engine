"""`output_type`: how a generated column is printed, as an F06 declared-casting cast at the Arrow boundary.

The option compiles once to a parse spec of the declared-casting catalog (`int`, `decimal(n)`,
`date`, `bool`), and the cast runs that descriptor's kernel, so bounds, precision and date shape
are the catalog's rules and no second format table exists here. What generation adds is the step
from a generated Python value to the kernel's input text (`to_lexeme`), including the one explicit
rounding (`decimal(n)` rounds half-even; an `int` target refuses a fraction rather than round it).

The cast runs after a column is finished (the formula post-pass and the null mask included) and
only where Arrow takes the values, so same-table readers (`derived`, `formula`) keep seeing the
generator's own values. Two call sites use it, `cast_output_columns` for whole tables and
`cast_output_values` for streamed batches. A cell that cannot be cast fails the run with
`output_type_cast_failed`, naming the table, column, target type and the kernel's reason code and
never the value.
"""

from __future__ import annotations

import numbers
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
from typing import Any

import numpy as np
import pyarrow as pa

from decoy_engine import declared_casting, reasons
from decoy_engine.config import parse_output_type
from decoy_engine.declared_casting import CastOutcome
from decoy_engine.reasons import ReasonCode

# A Decimal or date object is wider than the 8 bytes a plain number is priced at.
_TYPED_PAYLOAD_BYTES = 64
_ISO_DATETIME = re.compile(
    r"([0-9]{4}-[0-9]{2}-[0-9]{2})[T ][0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]{1,9})?)?"
    r"(?:Z|[+-][0-9]{2}(?::?[0-9]{2})?)?",
    re.ASCII,
)
_PLAIN_NUMBER = re.compile(r"[+-]?[0-9]{1,100}(?:\.[0-9]{1,100})?", re.ASCII)


def _invalid_reason(kind: str) -> ReasonCode:
    """The kernel's own invalid-value code for the kind, for a value that has no text to try."""
    if kind == "int":
        return reasons.runtime.DECLARED_INTEGER_INVALID
    if kind == "decimal":
        return reasons.runtime.DECLARED_DECIMAL_INVALID
    if kind == "date":
        return reasons.runtime.DECLARED_DATE_INVALID
    return reasons.runtime.DECLARED_BOOLEAN_INVALID


@dataclass(frozen=True)
class OutputType:
    """One compiled `output_type`: its text, kind, scale, parse spec and Arrow type."""

    text: str
    kind: str
    scale: int | None
    spec: Mapping[str, Any] = field(compare=False)
    arrow_type: pa.DataType = field(compare=False)


@lru_cache(maxsize=256)
def _compile(text: str) -> OutputType | None:
    parsed = parse_output_type(text)
    if parsed is None:
        return None
    kind, scale = parsed
    spec = declared_casting.generated_value_spec(kind, scale)
    arrow = declared_casting.arrow_type_for_spec(declared_casting.validate_format_spec(spec))
    return OutputType(text, kind, scale, spec, arrow)


def output_type_of(col: Mapping[str, Any]) -> OutputType | None:
    """The column's compiled `output_type`, or None when it declares none."""
    text = col.get("output_type")
    return _compile(text) if isinstance(text, str) else None


def output_types(columns: Iterable[Mapping[str, Any]] | None) -> dict[str, OutputType]:
    """`{column name: OutputType}` for each column of `columns` that declares one."""
    found: dict[str, OutputType] = {}
    for col in columns or ():
        out = output_type_of(col)
        if out is not None:
            found[str(col["name"])] = out
    return found


def output_arrow_type(col: Mapping[str, Any]) -> pa.DataType | None:
    """The Arrow type the column is emitted as, or None when it declares no `output_type`."""
    out = output_type_of(col)
    return out.arrow_type if out is not None else None


def output_payload_bound(bound: int | None) -> int | None:
    """A column's value-width bound once its values are typed objects."""
    return None if bound is None else max(bound, _TYPED_PAYLOAD_BYTES)


def _is_bool(value: Any) -> bool:
    return isinstance(value, (bool, np.bool_))


def _is_int(value: Any) -> bool:
    return isinstance(value, numbers.Integral) and not _is_bool(value)


def _is_float(value: Any) -> bool:
    return isinstance(value, (float, np.floating))


def _int_text(value: Any) -> str | None:
    if _is_int(value):
        return str(int(value))
    if _is_float(value):
        number = float(value)
        return str(int(number)) if number.is_integer() else None
    if isinstance(value, Decimal):
        return str(int(value)) if value.is_finite() and value == value.to_integral_value() else None
    if isinstance(value, str):
        return value
    return None


def _decimal_of(value: Any) -> Decimal | None:
    if _is_bool(value):
        return None
    if _is_int(value):
        return Decimal(int(value))
    if _is_float(value):
        return Decimal(repr(float(value)))  # the float's shortest repr, not its binary expansion
    if isinstance(value, Decimal):
        return value
    if isinstance(value, str) and _PLAIN_NUMBER.fullmatch(value):
        return Decimal(value)
    return None


def _date_text(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        # A faker datetime is text by the time it is cast; its date part is the date.
        match = _ISO_DATETIME.fullmatch(value)
        return match.group(1) if match else value
    return None


def _bool_text(value: Any) -> str | None:
    if _is_bool(value):
        return "true" if value else "false"
    if _is_int(value) and value in (0, 1):
        return str(int(value))
    return value if isinstance(value, str) else None


def to_lexeme(value: Any, out: OutputType) -> CastOutcome:
    """The kernel's input text for one generated value, or a failing outcome.

    `int` takes an integer, an integral float or Decimal, or text; a fraction is refused, never
    rounded, and a bool is not an int (use `decimal(0)` to round). `decimal(n)` takes any number or
    plain numeric text and rounds half-even to `n` places on the value's decimal digits. `date`
    takes a date, a datetime's date part, or ISO text. `bool` takes a bool, 0 or 1, or text.
    """
    text: str | None
    if out.kind == "int":
        text = _int_text(value)
    elif out.kind == "decimal":
        number = _decimal_of(value)
        if number is None:
            text = None
        else:
            rounded = declared_casting.round_to_scale(number, out.scale or 0)
            if not rounded.ok:
                return rounded
            text = format(rounded.value, "f")
    elif out.kind == "date":
        text = _date_text(value)
    else:
        text = _bool_text(value)
    if text is None:
        return CastOutcome(False, None, _invalid_reason(out.kind))
    return CastOutcome(True, text, None)


def _cast_one(value: Any, out: OutputType, parse: Any) -> CastOutcome:
    lexeme = to_lexeme(value, out)
    return parse(lexeme.value) if lexeme.ok else lexeme


def cast_output_values(
    values: Sequence[Any], out: OutputType, *, table: str, column: str
) -> list[Any]:
    """`values` cast to the column's output type; None stays None.

    Raises `ExecutionError(output_type_cast_failed)` at the first cell that cannot be cast, naming
    the table, column, target type and the kernel's reason code only.
    """
    parse = declared_casting.compile_parser(out.spec)
    typed: list[Any] = []
    for value in values:
        if value is None:
            typed.append(None)
            continue
        outcome = _cast_one(value, out, parse)
        if not outcome.ok:
            _refuse(table, column, out, outcome)
        typed.append(outcome.value)
    return typed


def _refuse(table: str, column: str, out: OutputType, outcome: CastOutcome) -> None:
    from decoy_engine.execution import ExecutionError

    raise ExecutionError(
        code=reasons.runtime.OUTPUT_TYPE_CAST_FAILED,
        message=(
            f"table {table!r} column {column!r} has a value that cannot be cast to "
            f"output_type {out.text!r} ({outcome.reason})."
        ),
    )


def cast_output_columns(
    data: dict[str, list[Any]], declared: Mapping[str, OutputType], *, table_name: str
) -> dict[str, list[Any]]:
    """`data` with each declared column cast; the other columns pass through untouched."""
    if not declared:
        return data
    return {
        name: cast_output_values(values, declared[name], table=table_name, column=name)
        if name in declared
        else values
        for name, values in data.items()
    }

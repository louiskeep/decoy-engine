"""Value checks for the `constant` and `distribution` generate types.

Callables for the option registry (`_generate_options`): each takes the submitted value and the
column's flat options (plus its `type`) and returns ``(relative location, reason code)`` pairs.
Nothing here quotes a submitted value. The family a `distribution` column chose is read from the
options; the table of which keys a family takes lives in
`_generate_options.CONDITIONAL_REQUIRED`.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from typing import Any

from decoy_engine import reasons

__all__ = [
    "clip_bound",
    "constant_value",
    "finite",
    "high_bound",
]

Problem = tuple[tuple[str | int, ...], str]

_TYPE = str(reasons.config.GENERATE_OPTION_TYPE)
_RANGE = str(reasons.config.GENERATE_OPTION_RANGE)

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

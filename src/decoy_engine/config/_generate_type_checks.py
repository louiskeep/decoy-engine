"""Value checks for the `constant` and `distribution` generate types.

Callables for the option registry (`_generate_options`): each takes the submitted value and the
column's flat options (plus its `type`) and returns ``(relative location, reason code)`` pairs.
Nothing here quotes a submitted value. The family a `distribution` column chose is read from the
options, so one key shared by several families (`mean`) gets the range rule of the family in
force; the table of which keys a family takes lives in `_generate_options.CONDITIONAL_REQUIRED`.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from typing import Any

from decoy_engine import reasons

__all__ = [
    "INT_FAMILIES",
    "POISSON_MEAN_MAX",
    "ZIPF_SUPPORT_MAX",
    "clip_bound",
    "constant_value",
    "finite",
    "high_bound",
    "mean_value",
    "positive",
    "zipf_support",
]

Problem = tuple[tuple[str | int, ...], str]

_TYPE = str(reasons.config.GENERATE_OPTION_TYPE)
_RANGE = str(reasons.config.GENERATE_OPTION_RANGE)

# Families whose draws are integers: their clip bounds must be ints too.
INT_FAMILIES = frozenset({"poisson", "zipf"})
POISSON_MEAN_MAX = 1_000_000_000
ZIPF_SUPPORT_MAX = 1_000_000
_INT64_MIN, _INT64_MAX = -(2**63), 2**63 - 1

_OK: list[Problem] = []


def _as_float(value: Any) -> float | None:
    """The value as a finite float, or None when it is not one (an int too large to convert)."""
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _is_number(value: Any) -> bool:
    return type(value) in (int, float)


def finite(value: Any, _: Mapping[str, Any]) -> list[Problem]:
    """A finite int or float; a bool is not a number."""
    if not _is_number(value):
        return [((), _TYPE)]
    return _OK if _as_float(value) is not None else [((), _RANGE)]


def positive(value: Any, extras: Mapping[str, Any]) -> list[Problem]:
    if problems := finite(value, extras):
        return problems
    return _OK if value > 0 else [((), _RANGE)]


def mean_value(value: Any, extras: Mapping[str, Any]) -> list[Problem]:
    """`mean` by family: any finite value for normal, positive for exponential, a bounded rate
    for poisson. An unknown family is reported on `family` itself, so only the type shows here."""
    family = extras.get("family")
    if family == "exponential":
        return positive(value, extras)
    if family == "poisson":
        if problems := positive(value, extras):
            return problems
        return _OK if value <= POISSON_MEAN_MAX else [((), _RANGE)]
    return finite(value, extras)


def high_bound(value: Any, extras: Mapping[str, Any]) -> list[Problem]:
    """`high` of a uniform column: finite and above `low` once `low` is itself a number."""
    if problems := finite(value, extras):
        return problems
    low = extras.get("low")
    if _is_number(low) and _as_float(low) is not None and value <= low:
        return [((), _RANGE)]
    return _OK


def zipf_support(value: Any, _: Mapping[str, Any]) -> list[Problem]:
    """`n` of a zipf column: an int in 1..ZIPF_SUPPORT_MAX."""
    if type(value) is not int:
        return [((), _TYPE)]
    return _OK if 1 <= value <= ZIPF_SUPPORT_MAX else [((), _RANGE)]


def clip_bound(which: str) -> Callable[[Any, Mapping[str, Any]], list[Problem]]:
    """Check for `min` or `max`: an int on the integer families, a finite number elsewhere; and
    `min <= max` once both are numbers (reported on `max`)."""

    def check(value: Any, extras: Mapping[str, Any]) -> list[Problem]:
        if extras.get("family") in INT_FAMILIES:
            if type(value) is not int:
                return [((), _TYPE)]
            if not _INT64_MIN <= value <= _INT64_MAX:
                return [((), _RANGE)]
        if problems := finite(value, extras):
            return problems
        other = extras.get("min")
        if which == "max" and _is_number(other) and _as_float(other) is not None and other > value:
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

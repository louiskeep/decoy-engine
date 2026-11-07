"""Construction-time check that a report holds only value-free builtins.

Every report dataclass calls ``check_report_values`` from ``__post_init__``,
so a numpy scalar, a stray string or any other object cannot be stored in a
report: the wrong form fails at construction, not in review.
"""

from __future__ import annotations

import dataclasses
from typing import Any

_SCALARS = (int, float, bool, type(None))


def check_report_values(report: Any, tokens: frozenset[str] = frozenset()) -> None:
    """Raise ``TypeError`` unless every field is a builtin scalar, a tuple of
    ``(int, int)`` pairs, or one of ``tokens``. Exact types: ``np.int64`` and
    ``str`` subclasses are refused."""
    for field in dataclasses.fields(report):
        value = getattr(report, field.name)
        kind = type(value)
        if kind in _SCALARS:
            continue
        if kind is str and value in tokens:
            continue
        if kind is tuple and all(
            type(pair) is tuple and len(pair) == 2 and all(type(x) is int for x in pair)
            for pair in value
        ):
            continue
        raise TypeError(
            f"{type(report).__name__}.{field.name} must be a builtin count or measure, "
            f"not {kind.__name__}"
        )

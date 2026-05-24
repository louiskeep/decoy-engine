"""Shared safe expression evaluator.

All Python eval() calls in the engine route through safe_eval() here.
Keeping them in one place makes the allowlist auditable and the noqa
suppression traceable to one site.

Two scope presets:

  MASK_GLOBALS   -- used by FormulaStrategy (mask side). Includes re,
                    common type coercions, numeric helpers, and basic RNG.
  BASE_GLOBALS   -- used by generation-side formula paths. Suppresses
                    builtins only; the full generation scope (faker helpers,
                    hash, date utilities) lives in
                    ColumnGenerator._formula_scope and is passed as locals.

The pandas DataFrame.eval() used by derive.py is intentionally excluded:
it runs the pandas/NumPy expression engine, not CPython eval(), and has
a distinct security profile that is owned by the derive op directly.
"""

from __future__ import annotations

import random as _random
import re as _re
from typing import Any

BASE_GLOBALS: dict[str, Any] = {"__builtins__": {}}

MASK_GLOBALS: dict[str, Any] = {
    "__builtins__": {},
    "re": _re,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "len": len,
    "round": round,
    "abs": abs,
    "min": min,
    "max": max,
    "randint": _random.randint,
    "choice": _random.choice,
}


def safe_eval(
    expr: str,
    globals_: dict[str, Any],
    locals_: dict[str, Any],
) -> Any:
    """Evaluate *expr* under the given scope.

    *globals_* should be ``MASK_GLOBALS`` or ``BASE_GLOBALS`` from this
    module. The function is intentionally thin -- its value is that every
    engine Python eval() call imports from one place, making auditing and
    the noqa suppression traceable.
    """
    return eval(expr, globals_, locals_)  # noqa: S307

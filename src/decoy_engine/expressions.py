"""Shared safe expression evaluator.

All Python eval() calls in the engine route through safe_eval() here.
Keeping them in one place makes the allowlist auditable and the noqa
suppression traceable to one site.

Two scope presets:

  MASK_GLOBALS   -- used by FormulaStrategy (mask side). Includes re,
                    common type coercions, numeric helpers, and basic RNG.
                    QA-1 M21 (2026-06-01): the RNG bindings (randint,
                    choice) are exposed as module-level callables for
                    legacy callers, but new callers should use
                    `make_mask_globals(rng)` to bind a per-formula
                    Random instance so two formula strategies in the
                    same job no longer share module-global RNG state.
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
    # QA-1 M21 (2026-06-01): module-level bindings retained for backward
    # compatibility but tagged as the dangerous path. Two formula
    # strategies in the same job calling these bindings share
    # module-global random state; column B's output depends on column
    # A's execution order. The make_mask_globals factory below returns
    # an isolated scope per call site.
    "randint": _random.randint,
    "choice": _random.choice,
    "random": _random.random,
}


def make_mask_globals(rng: _random.Random) -> dict[str, Any]:
    """QA-1 M21 (2026-06-01): construct a MASK_GLOBALS scope whose
    RNG bindings target the passed-in Random instance.

    The mask-side FormulaStrategy should construct a per-formula
    `random.Random(formula_seed)` and pass it to this factory. The
    returned dict is byte-identical to `MASK_GLOBALS` except for the
    three RNG bindings, which now read from the instance instead of
    module-global state.
    """
    scope = dict(MASK_GLOBALS)
    scope["randint"] = rng.randint
    scope["choice"] = rng.choice
    scope["random"] = rng.random
    return scope


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

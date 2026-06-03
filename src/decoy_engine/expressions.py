"""Shared safe expression evaluator.

All user-supplied `formula` expressions in the engine route through
`safe_eval()` here, evaluated under simpleeval's restricted single-expression
sandbox -- NOT CPython `eval()`.

SEC.1 / C1 (2026-06-03): this module previously called
`eval(expr, {"__builtins__": {}}, locals)`. Emptying `__builtins__` is a known
non-boundary: `().__class__.__bases__[0].__subclasses__()` walks the object
graph to reach `os`/`subprocess`, so any formula author had remote code
execution. simpleeval closes the whole class by design -- it rejects
`_`-prefixed name/attribute access (so the dunder traversal cannot start),
the `.format`/`.format_map` format-string escape, module references in scope,
and any name not explicitly placed in scope. We do not roll our own AST
sandbox (the established-methodology rule): simpleeval is the ecosystem's
standard "safely evaluate one user-supplied expression" library, the same way
numexpr is the standard for vectorized expressions (used by derive/filter/when,
see below) and jsonpath-ng is the standard for JSON paths.

Two scope presets:

  MASK_GLOBALS   -- used by FormulaStrategy (mask side). Includes a safe `re`
                    proxy (see _SafeRe), common type coercions, numeric
                    helpers, and basic RNG. New callers should use
                    `make_mask_globals(rng)` to bind a per-formula Random
                    instance so two formula strategies in the same job do not
                    share module-global RNG state.
  BASE_GLOBALS   -- used by generation-side formula paths. The full generation
                    scope (faker helpers, hash, date utilities) lives in
                    ColumnGenerator._formula_scope and is passed as locals.

The pandas DataFrame.eval() used by derive.py is intentionally excluded: it
runs the pandas/NumPy expression engine, not this evaluator, and has a distinct
security profile owned by the derive op directly.
"""

from __future__ import annotations

import random as _random
import re as _re
from typing import Any

from simpleeval import EvalWithCompoundTypes


class _SafeRe:
    """A non-module proxy exposing `re`'s safe public API.

    simpleeval refuses to reference a *module* in scope (a module is an
    attribute-access escape surface), so we cannot hand it the real `re`
    module. This proxy is an ordinary object exposing the regex functions and
    flag constants that formulas use, with identical `re.sub(...)` /
    `re.search(...)` spelling. simpleeval still blocks `_`-prefixed access on
    it, so the proxy is not a path back to the module or its internals.
    """

    sub = staticmethod(_re.sub)
    subn = staticmethod(_re.subn)
    search = staticmethod(_re.search)
    match = staticmethod(_re.match)
    fullmatch = staticmethod(_re.fullmatch)
    findall = staticmethod(_re.findall)
    finditer = staticmethod(_re.finditer)
    split = staticmethod(_re.split)
    compile = staticmethod(_re.compile)
    escape = staticmethod(_re.escape)
    # Flag constants (both short and long spellings, mirroring `re`).
    I = IGNORECASE = _re.IGNORECASE
    M = MULTILINE = _re.MULTILINE
    S = DOTALL = _re.DOTALL
    X = VERBOSE = _re.VERBOSE
    A = ASCII = _re.ASCII


_SAFE_RE = _SafeRe()

# The "__builtins__": {} entries are vestigial: simpleeval never exposes
# builtins, and safe_eval drops the key before evaluating. They are kept so the
# dict shape stays byte-identical for existing make_mask_globals callers.
BASE_GLOBALS: dict[str, Any] = {"__builtins__": {}}

MASK_GLOBALS: dict[str, Any] = {
    "__builtins__": {},
    "re": _SAFE_RE,
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
    # compatibility but tagged as the dangerous path. Two formula strategies in
    # the same job calling these share module-global random state; column B's
    # output depends on column A's execution order. make_mask_globals returns an
    # isolated scope per call site.
    "randint": _random.randint,
    "choice": _random.choice,
    "random": _random.random,
}


def make_mask_globals(rng: _random.Random) -> dict[str, Any]:
    """QA-1 M21 (2026-06-01): construct a MASK_GLOBALS scope whose RNG bindings
    target the passed-in Random instance.

    The mask-side FormulaStrategy should construct a per-formula
    `random.Random(formula_seed)` and pass it to this factory. The returned
    dict is identical to `MASK_GLOBALS` except for the three RNG bindings,
    which now read from the instance instead of module-global state.
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
    """Evaluate *expr* under the given scope in simpleeval's restricted sandbox.

    *globals_* should be ``MASK_GLOBALS`` / ``BASE_GLOBALS`` (or a
    ``make_mask_globals`` result). The scope's callables become simpleeval
    ``functions`` (callable by bare name, e.g. ``str(...)``, ``randint(...)``)
    and the whole scope becomes ``names`` (referenceable operands, e.g.
    ``value``, the ``re`` proxy). The literal ``"__builtins__"`` entry is
    dropped (simpleeval never exposes builtins).

    Attribute access on operand values is permitted (``value.upper()``,
    ``re.search(...).group()``), but simpleeval blocks ``_``/``func_``-prefixed
    access, the ``.format`` family, module references, and any name not in
    scope -- which is what closes the former ``eval()`` RCE while preserving the
    formula surface. A malformed expression still raises ``SyntaxError`` (via
    ``ast.parse``); a blocked or undefined name raises a simpleeval
    ``InvalidExpression`` subclass.
    """
    scope = {k: v for k, v in globals_.items() if k != "__builtins__"}
    scope.update(locals_)
    functions = {k: v for k, v in scope.items() if callable(v)}
    return EvalWithCompoundTypes(names=scope, functions=functions).eval(expr)

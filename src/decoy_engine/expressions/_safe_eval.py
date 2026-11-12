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

import ast
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
    I = IGNORECASE = _re.IGNORECASE  # noqa: E741 - mirrors re.I flag name
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
    # F16a (2026-06-26): the RNG bindings (randint/choice/random) are deliberately
    # NOT in the base scope. They are added ONLY by make_mask_globals(rng), bound
    # to a per-formula isolated Random. Binding them here to module-global _random
    # was a shared-state footgun: two formula strategies in the same job shared
    # process-global random state, so column B's output depended on column A's
    # execution order and the stream was non-deterministic across runs. Evaluating
    # a formula that calls randint/choice/random against the bare MASK_GLOBALS now
    # raises (the name is undefined) instead of silently sharing state; the only
    # supported RNG path is make_mask_globals.
}


def make_mask_globals(rng: _random.Random) -> dict[str, Any]:
    """QA-1 M21 (2026-06-01): construct a MASK_GLOBALS scope whose RNG bindings
    target the passed-in Random instance.

    The mask-side FormulaStrategy should construct a per-formula
    `random.Random(formula_seed)` and pass it to this factory. The returned
    dict is `MASK_GLOBALS` PLUS four RNG bindings (randint/choice/random/gauss)
    bound to the passed-in instance. The bare `MASK_GLOBALS` no longer carries
    them (F16a), so this factory is the only path that exposes RNG to a formula.

    `gauss` is included for parity with the generation-side `_formula_scope`
    (which also binds gauss to the per-row rng). Mask formulas that need a
    normal-distribution draw can call `gauss(mu, sigma)` via this scope.
    """
    scope = dict(MASK_GLOBALS)
    scope["randint"] = rng.randint
    scope["choice"] = rng.choice
    scope["random"] = rng.random
    scope["gauss"] = rng.gauss
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


# ---- admissibility ------------------------------------------------------------------------------
#
# simpleeval parses any Python expression and only refuses a node type when it reaches it at
# run time, so a lambda or a walrus used to compile and then generate a column of nulls. The
# check below reads the evaluator's own dispatch table (``nodes``) and operator table, so it
# cannot drift from what ``safe_eval`` can run. Config and plan validation both call it.

_probe = EvalWithCompoundTypes(names={}, functions={})
# Node types simpleeval dispatches on. Frozen at import: the instance is only a way to read them.
EVALUATED_NODE_TYPES: frozenset[type] = frozenset(_probe.nodes)
EVALUATED_OPERATOR_TYPES: frozenset[type] = frozenset(_probe.operators)
del _probe

# Nodes the evaluator never dispatches on but reaches through a parent that it does handle:
# the parse root, load/store contexts, the and/or marker of a BoolOp, and the clauses of a
# comprehension.
_STRUCTURAL = (ast.Expression, ast.expr_context, ast.And, ast.Or, ast.comprehension)


def formula_is_evaluable(tree: ast.AST) -> bool:
    """True when ``safe_eval`` can run every node of *tree* (an ``ast.parse(..., "eval")`` result).

    A node outside the evaluator's dispatch table (lambda, walrus, set comprehension, await,
    ...) is refused, as are the cases the evaluator handles only in one position: ``*x`` inside
    a list display, a call through a name or attribute, and ``f(**x)``.
    """
    starred_ok: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.List):
            starred_ok |= {id(e) for e in node.elts if isinstance(e, ast.Starred)}
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, (ast.Name, ast.Attribute)):
                return False
        elif isinstance(node, ast.keyword) and node.arg is None:
            return False
    for node in ast.walk(tree):
        kind = type(node)
        if isinstance(node, ast.Starred):
            if id(node) not in starred_ok:
                return False
        elif isinstance(node, (ast.operator, ast.unaryop, ast.cmpop)):
            if kind not in EVALUATED_OPERATOR_TYPES:
                return False
        elif kind not in EVALUATED_NODE_TYPES and not isinstance(node, _STRUCTURAL):
            return False
    return True

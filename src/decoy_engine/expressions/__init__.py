"""Shared expression evaluators for decoy_engine.

Two distinct evaluators live here, serving different strategies:

safe_eval / BASE_GLOBALS / MASK_GLOBALS
  simpleeval-backed sandbox for the ``formula`` strategy. Accepts
  arbitrary Python expressions within the simpleeval allowlist. The
  original security rationale is in ``_safe_eval.py``.

compile_expr / evaluate
  Lark-backed CLOSED-VOCABULARY parser for the ``derived`` and
  ``case_when`` strategies. Accepts ONLY the explicit operator set
  defined in grammar.lark (arithmetic, comparison, logical, membership,
  concat, days_between, ternary, column refs, literals). Any expression
  outside that set raises ValidationError at compile time, before any
  row evaluation. No dynamic code execution anywhere in this path.

Pattern: Lark EBNF closed-grammar sandbox (lark-parser/lark, MIT).
See: https://github.com/lark-parser/lark

The two APIs are intentionally separate: formula uses simpleeval's
permissive-but-sandboxed approach; derived/case_when use the Lark
closed-grammar approach (zero dynamic execution surface).
"""

from __future__ import annotations

# Re-export the simpleeval-based formula API so all existing callers
# (generators/_formula.py, transforms/formula.py, etc.) see the same
# public names as before the flat expressions.py was converted to a
# package. This is a strict drop-in replacement.
from decoy_engine.expressions._safe_eval import (
    BASE_GLOBALS,
    MASK_GLOBALS,
    _SafeRe,
    make_mask_globals,
    safe_eval,
)

# Lark closed-grammar API for derived / case_when strategies.
from decoy_engine.expressions._lark_parser import (
    CompiledExpression,
    compile_expr,
    evaluate,
)

__all__ = [
    # simpleeval formula API (unchanged public surface)
    "safe_eval",
    "BASE_GLOBALS",
    "MASK_GLOBALS",
    "make_mask_globals",
    "_SafeRe",
    # Lark derived/case_when API (new in SP-06)
    "compile_expr",
    "evaluate",
    "CompiledExpression",
]

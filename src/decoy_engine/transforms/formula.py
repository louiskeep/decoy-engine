# decoy_engine/strategies/formula.py
import re as _re
import random as _random
from typing import Any, Dict

import pandas as pd

from decoy_engine.transforms.base import BaseMaskingStrategy


class FormulaStrategy(BaseMaskingStrategy):
    """Transform existing column values via a Python expression.

    Single-mode evaluator (F0 of the formula consolidation): every formula
    is a Python expression. Users who want template-style ``"Hello {x}"``
    substitution write ``f"Hello {x}"`` themselves — that's already Python.

    Per-row scope:
      - ``value`` — the current cell's original content.
      - ``re``, ``str``, ``int``, ``float``, ``bool``, ``len``, ``round``,
        ``abs``, ``min``, ``max`` — common utilities.
      - ``randint``, ``choice`` — random helpers (NOT FK-safe; the strategy
        is labeled ``'depends'`` because of these).

    Cross-column ``references: [...]`` is NOT supported on the mask side
    yet — mask's ``apply()`` only sees one column at a time, so reading
    sibling columns requires a strategy-API change. That lands in the F1
    tier of plans/2026-05-08-formula-expansion.md. Generate's formula
    already supports references via the post-pass evaluator.
    """

    _SAFE_GLOBALS: Dict[str, Any] = {
        '__builtins__': {},
        're': _re,
        'str': str, 'int': int, 'float': float, 'bool': bool,
        'len': len, 'round': round, 'abs': abs,
        'min': min, 'max': max,
        'randint': _random.randint,
        'choice': _random.choice,
    }

    def apply(self, column: pd.Series, rule: Dict[str, Any]) -> pd.Series:
        expr = rule.get('formula', '')
        if not expr:
            self.logger.warning(
                f"formula strategy on '{rule.get('column', 'unnamed')}' "
                f"missing 'formula' field; leaving column unchanged"
            )
            return column.copy()
        return column.apply(lambda v: v if pd.isna(v) else self._eval(expr, v))

    def _eval(self, expr: str, value: Any) -> Any:
        return eval(expr, self._SAFE_GLOBALS, {'value': value})  # noqa: S307

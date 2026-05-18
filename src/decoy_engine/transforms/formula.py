# decoy_engine/strategies/formula.py
from typing import Any, Dict

import pandas as pd

from decoy_engine.expressions import MASK_GLOBALS, safe_eval
from decoy_engine.transforms.base import BaseMaskingStrategy


class FormulaStrategy(BaseMaskingStrategy):
    """Transform existing column values via a Python expression.

    Single-mode evaluator (F0 of the formula consolidation): every formula
    is a Python expression. Users who want template-style ``"Hello {x}""
    substitution write ``f"Hello {x}"`` themselves -- that's already Python.

    Per-row scope (defined in :data:`decoy_engine.expressions.MASK_GLOBALS`):
      - ``value`` -- the current cell's original content.
      - ``re``, ``str``, ``int``, ``float``, ``bool``, ``len``, ``round``,
        ``abs``, ``min``, ``max`` -- common utilities.
      - ``randint``, ``choice`` -- random helpers (NOT FK-safe; the strategy
        is labeled ``'depends'`` because of these).

    Cross-column ``references: [...]`` is NOT supported on the mask side
    yet -- mask's ``apply()`` only sees one column at a time, so reading
    sibling columns requires a strategy-API change. That lands in the F1
    tier of plans/2026-05-08-formula-expansion.md. Generate's formula
    already supports references via the post-pass evaluator.

    All eval() calls route through
    :func:`decoy_engine.expressions.safe_eval`.
    """

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
        return safe_eval(expr, MASK_GLOBALS, {'value': value})

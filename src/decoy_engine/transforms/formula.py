# decoy_engine/strategies/formula.py
import re as _re
import random as _random
from typing import Any, Dict

import pandas as pd

from decoy_engine.transforms.base import BaseMaskingStrategy


class FormulaStrategy(BaseMaskingStrategy):
    """Transform existing column values using a Python expression or f-string template.

    Supported formula_types: 'basic' (eval expression), 'template' (f-string).
    'value' is always the current cell's original content.
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
        formula_type = rule.get('formula_type', 'basic')
        expr = rule.get('formula', '')
        if formula_type == 'template':
            fn = lambda v: self._apply_template(expr, v)
        else:
            fn = lambda v: self._apply_basic(expr, v)
        return column.apply(lambda v: v if pd.isna(v) else fn(v))

    def _apply_basic(self, expr: str, value: Any) -> Any:
        return eval(expr, self._SAFE_GLOBALS, {'value': value})  # noqa: S307

    def _apply_template(self, expr: str, value: Any) -> str:
        return eval(f"f'''{expr}'''", self._SAFE_GLOBALS, {'value': value})  # noqa: S307

import hashlib
import random
from typing import Any

import pandas as pd

from decoy_engine.expressions import make_mask_globals, safe_eval
from decoy_engine.transforms.base import BaseMaskingStrategy


class FormulaStrategy(BaseMaskingStrategy):
    """Transform existing column values via a Python expression.

    Single-mode evaluator (F0 of the formula consolidation): every formula
    is a Python expression. Users who want template-style ``"Hello {x}""
    substitution write ``f"Hello {x}"`` themselves -- that's already Python.

    Per-row scope (built via
    :func:`decoy_engine.expressions.make_mask_globals` per formula):
      - ``value`` -- the current cell's original content.
      - ``re``, ``str``, ``int``, ``float``, ``bool``, ``len``, ``round``,
        ``abs``, ``min``, ``max`` -- common utilities.
      - ``randint``, ``choice``, ``random`` -- bound to a per-formula
        ``random.Random`` instance (QA-1 M21 closure, 2026-06-01). Two
        formula strategies in the same mask job no longer share
        module-global RNG state; column B's output is a pure function
        of (formula text, the column's deterministic seed) and no
        longer depends on column A's execution order.

    Cross-column ``references: [...]`` is NOT supported on the mask side
    yet; that lands in the F1 tier of plans/2026-05-08-formula-expansion.md.
    Generate's formula already supports references via the post-pass
    evaluator.

    All eval() calls route through
    :func:`decoy_engine.expressions.safe_eval`.
    """

    def apply(self, column: pd.Series, rule: dict[str, Any]) -> pd.Series:
        expr = rule.get("formula", "")
        if not expr:
            self.logger.warning(
                f"formula strategy on '{rule.get('column', 'unnamed')}' "
                f"missing 'formula' field; leaving column unchanged"
            )
            return column.copy()
        # QA-1-followup M21 (2026-06-01): per-formula Random instance.
        # Seed comes from the formula text + column name so the same
        # formula on the same column produces byte-identical output
        # across runs (deterministic), but two different formulas in
        # the same job no longer share module-global RNG state.
        col_name = rule.get("column", "unnamed")
        seed_material = f"{col_name}|{expr}".encode("utf-8")
        formula_seed = int(hashlib.sha256(seed_material).hexdigest()[:16], 16)
        rng = random.Random(formula_seed)
        scope = make_mask_globals(rng)
        return column.apply(
            lambda v: v if pd.isna(v) else safe_eval(expr, scope, {"value": v})
        )

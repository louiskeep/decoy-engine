"""Shuffle masking strategy.

Backend-agnostic — works equally well for both numpy-backed and
arrow-backed pandas Series. The earlier `.values.copy() + random.shuffle()`
implementation collapsed to a Python-level Fisher-Yates loop on
arrow-backed input (~640x slower at 100k rows; projected hours at 1M+).
The current implementation permutes integer indices via numpy and
takes by .iloc — both dispatch to fast C paths regardless of dtype
backend.
"""

import numpy as np
import pandas as pd
from typing import Any, Dict

from decoy_engine.transforms.base import BaseMaskingStrategy


class ShuffleStrategy(BaseMaskingStrategy):
    """Randomly shuffles values within a column. Preserves the
    distribution of values while breaking the association with records.
    Null positions are preserved."""

    def apply(self, column: pd.Series, rule: Dict[str, Any]) -> pd.Series:
        self.logger.debug("Applying shuffle mask to column")

        seed = rule.get("seed", self.seed)

        na_mask = column.isna()
        non_na = column[~na_mask]
        non_na_count = len(non_na)

        self.logger.debug(
            f"Shuffling {non_na_count} non-null values, "
            f"preserving {na_mask.sum()} null positions"
        )

        # Permute integer indices via numpy (C-level on a small int array),
        # then take by .iloc — both fast on numpy-backed and arrow-backed
        # pandas. .to_numpy() materializes positionally so the subsequent
        # boolean-mask assignment doesn't trigger pandas' index alignment
        # and silently undo the shuffle.
        rng = np.random.default_rng(seed)
        indices = rng.permutation(non_na_count)
        shuffled_values = non_na.iloc[indices].to_numpy()

        shuffled = pd.Series(index=column.index, dtype=column.dtype)
        shuffled[~na_mask] = shuffled_values
        shuffled[na_mask] = None

        self._log_stats(column, shuffled, rule)
        return shuffled

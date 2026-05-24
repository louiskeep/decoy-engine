"""
Passthrough masking strategy for the decoy_engine package.
Keeps the original values unchanged.
"""

from typing import Any

import pandas as pd

from decoy_engine.transforms.base import BaseMaskingStrategy


class PassthroughStrategy(BaseMaskingStrategy):
    """
    Masking strategy that keeps the original values unchanged.
    Useful for non-sensitive data or fields needed for reference.
    """

    def apply(self, column: pd.Series, rule: dict[str, Any]) -> pd.Series:
        """
        Keeps the original values unchanged

        Args:
            column: Pandas Series to mask
            rule: Dictionary containing the masking rule configuration

        Returns:
            The original pandas Series unchanged
        """
        self.logger.debug("Applying passthrough mask (no changes)")

        # Simply return the original column
        return column

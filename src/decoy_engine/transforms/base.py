"""
Base class for masking strategies in the decoy_engine package.
"""

from abc import abstractmethod
from typing import Any

import pandas as pd

from decoy_engine.internal.base import MaskingStrategy


class BaseMaskingStrategy(MaskingStrategy):
    """
    Base class for all masking strategies.
    Implements common functionality and defines the interface.
    """

    def __init__(self, seed: int = 42, logger=None, derive_key=None):
        """
        Initialize the strategy with a seed for deterministic behavior

        Args:
            seed: Random seed for deterministic masking (legacy fallback)
            logger: Logger instance (optional)
            derive_key: Optional ``(info: str) -> bytes`` for keyed
                determinism. Keyed strategies prefer this when present.
        """
        super().__init__(seed, logger, derive_key=derive_key)

    @abstractmethod
    def apply(self, column: pd.Series, rule: dict[str, Any]) -> pd.Series:
        """
        Apply the masking strategy to a column

        Args:
            column: Pandas Series to mask
            rule: Dictionary containing the masking rule configuration

        Returns:
            Pandas Series with masked values
        """
        pass

    def _preserve_nulls(self, original_column: pd.Series, masked_column: pd.Series) -> pd.Series:
        """
        Preserve null values from the original column

        Args:
            original_column: Original pandas Series
            masked_column: Masked pandas Series

        Returns:
            Pandas Series with nulls preserved
        """
        # Create a mask for null values in the original column
        null_mask = original_column.isna()

        # Apply the mask to the result
        result = masked_column.copy()
        result[null_mask] = None

        return result

    def _log_stats(self, column: pd.Series, result: pd.Series, rule: dict[str, Any]) -> None:
        """
        Log statistics about the masking operation

        Args:
            column: Original pandas Series
            result: Masked pandas Series
            rule: Masking rule configuration
        """
        column_name = rule.get("column", "unnamed")
        strategy_name = self.strategy_name

        # Count non-null values
        non_null_count = column.count()

        # Count values that changed. Cast both sides to object first because
        # masking can change the column's dtype (hash on int64 -> string in
        # object dtype, faker on int -> string, etc.). Newer pandas dispatches
        # mixed-dtype `!=` to pyarrow, which has no kernel for cross-dtype
        # comparisons like (int64, string) and would raise. Object-on-object
        # falls back to Python equality, which is what this stats line wants.
        try:
            changed_mask = (column.astype(object) != result.astype(object)) & ~column.isna()
            changed_count = int(changed_mask.sum())
        except Exception as exc:
            # _log_stats is debug-only. Don't kill the mask run if the
            # comparison can't be computed for any reason.
            self.logger.debug(f"Couldn't compute change stats for '{column_name}': {exc}")
            changed_count = 0

        if non_null_count > 0:
            change_percentage = (changed_count / non_null_count) * 100
        else:
            change_percentage = 0

        self.logger.debug(f"Applied '{strategy_name}' strategy to column '{column_name}'")
        self.logger.debug(
            f"Masked {changed_count}/{non_null_count} values ({change_percentage:.1f}%)"
        )

        # Count unique values
        unique_original = column.nunique()
        unique_result = result.nunique()

        self.logger.debug(f"Unique values: {unique_original} original, {unique_result} masked")

    def validate_rule(self, rule: dict[str, Any]) -> None:
        """
        Validate that the rule contains all required fields for this strategy

        Args:
            rule: Dictionary containing the masking rule configuration

        Raises:
            ValueError: If rule validation fails
        """
        if "column" not in rule:
            raise ValueError(f"Rule for {self.strategy_name} strategy is missing 'column' field")

        # Check specific strategy type
        if rule.get("type") != self.strategy_name:
            rule_type = rule.get("type", "unknown")
            raise ValueError(
                f"Rule type '{rule_type}' does not match strategy '{self.strategy_name}'"
            )

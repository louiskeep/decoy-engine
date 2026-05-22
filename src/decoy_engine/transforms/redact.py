# decoy_engine/strategies/redact.py
"""
Redact masking strategy for the decoy_engine package.
Replaces values with a fixed redaction string.
"""

import pandas as pd
from typing import Dict, Any, Optional

from decoy_engine.transforms.base import BaseMaskingStrategy


class RedactStrategy(BaseMaskingStrategy):
    """
    Masking strategy that replaces all values with a fixed redaction string.
    Simple and effective for hiding sensitive data completely.
    """
    
    def apply(self, column: pd.Series, rule: Dict[str, Any]) -> pd.Series:
        """
        Replace all values in a column with a fixed redaction string.
        Null positions are preserved.

        Args:
            column: Pandas Series to mask
            rule: Dictionary containing the masking rule configuration

        Returns:
            Pandas Series with redacted values
        """
        redact_with = rule.get('redact_with', 'REDACTED')

        self.logger.debug(f"Applying redaction mask with value '{redact_with}'")

        # Drop any extension-dtype tag (e.g. int64[pyarrow] for a CSV
        # column the reader inferred as int) before .where() runs --
        # the redaction value is a string, and pandas extension-array
        # setitem path tries to cast the string to the original dtype
        # ("Could not convert 'REDACTED' to int64"). Casting to plain
        # object dtype here lets .where() see a python-object column
        # and write the string cleanly. Same pattern as date_shift.py
        # lines 108-109. The registry-level drop+reinsert covers the
        # back-assignment; this covers the per-strategy internal path.
        if pd.api.types.is_extension_array_dtype(column.dtype):
            column = column.astype(object)

        # Vectorized: where the value IS NA, keep it; otherwise replace
        # with the redaction string. Replaces a per-row Python loop that
        # existed mostly to do this exact branch — pandas handles it
        # natively at C speed.
        result = column.where(column.isna(), redact_with)

        self._log_stats(column, result, rule)
        non_null_count = result.count()
        self.logger.debug(f"Redacted {non_null_count} non-null values")
        return result
    
    def validate_rule(self, rule: Dict[str, Any]) -> None:
        """
        Validate that the rule contains all required fields for the redact strategy
        
        Args:
            rule: Dictionary containing the masking rule configuration
            
        Raises:
            ValueError: If rule validation fails
        """
        super().validate_rule(rule)
        
        # Check if redact_with is specified, if not, set default
        if 'redact_with' not in rule:
            rule['redact_with'] = 'REDACTED'
            self.logger.debug(f"Using default redact_with: 'REDACTED' for column '{rule['column']}'")
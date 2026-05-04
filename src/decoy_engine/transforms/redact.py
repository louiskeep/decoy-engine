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
        Replace all values in a column with a fixed redaction string
        
        Args:
            column: Pandas Series to mask
            rule: Dictionary containing the masking rule configuration
            
        Returns:
            Pandas Series with redacted values
        """
        # Get redaction text from rule or use default
        redact_with = rule.get('redact_with', 'REDACTED')
        
        self.logger.debug(f"Applying redaction mask with value '{redact_with}'")
        
        # Create a simple mapping that replaces all non-None values with the redaction text
        def redact_value(val):
            if val is None or pd.isna(val):
                return val
            return redact_with
        
        # Apply the function to the column
        result = column.apply(redact_value)
        
        # Log statistics
        self._log_stats(column, result, rule)
        
        # Count of non-null values
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
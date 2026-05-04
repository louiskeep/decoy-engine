# forge_engine/strategies/hash.py
"""
Hash masking strategy for the forge_engine package.
Replaces values with deterministic hash values.
"""

import pandas as pd
from typing import Dict, Any, Optional

from forge_engine.transforms.base import BaseMaskingStrategy
from forge_engine.utils.helpers import deterministic_hash


class HashStrategy(BaseMaskingStrategy):
    """
    Masking strategy that replaces values with deterministic hash strings.
    Ensures that the same input always produces the same hash.
    """
    
    def apply(self, column: pd.Series, rule: Dict[str, Any]) -> pd.Series:
        """
        Replace values with deterministic hash strings that can't be reversed
        
        Args:
            column: Pandas Series to mask
            rule: Dictionary containing the masking rule configuration
            
        Returns:
            Pandas Series with hashed values
        """
        # Get seed from rule or use default
        seed = rule.get('seed', self.seed)
        
        self.logger.debug(f"Applying hash mask with seed {seed}")
        
        # Apply deterministic hashing to each value
        def hash_value(val):
            if val is None or pd.isna(val):
                return val
            return deterministic_hash(str(val), seed)
        
        # Apply the function to the column
        result = column.apply(hash_value)
        
        # Log statistics
        self._log_stats(column, result, rule)
        
        return result
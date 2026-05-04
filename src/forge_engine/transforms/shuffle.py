# forge_engine/strategies/shuffle.py
"""
Shuffle masking strategy for the forge_engine package.
Randomly shuffles values within a column.
"""

import pandas as pd
import random
from typing import Dict, Any, Optional

from forge_engine.transforms.base import BaseMaskingStrategy


class ShuffleStrategy(BaseMaskingStrategy):
    """
    Masking strategy that randomly shuffles values within a column.
    Preserves the distribution of values while breaking the association with records.
    """
    
    def __init__(self, seed: int = 42, logger=None):
        """
        Initialize the shuffle strategy with seed for deterministic behavior
        
        Args:
            seed: Random seed for deterministic masking
            logger: Logger instance (optional)
        """
        super().__init__(seed, logger)
        # Set random seed for consistent shuffling
        random.seed(self.seed)
    
    def apply(self, column: pd.Series, rule: Dict[str, Any]) -> pd.Series:
        """
        Randomly shuffle values within a column while preserving null positions
        
        Args:
            column: Pandas Series to mask
            rule: Dictionary containing the masking rule configuration
            
        Returns:
            Pandas Series with shuffled values
        """
        self.logger.debug(f"Applying shuffle mask to column")
        
        # Get seed from rule or use default
        seed = rule.get('seed', self.seed)
        random.seed(seed)
        
        # Preserve None/NaN values in their positions
        na_mask = column.isna()
        non_na_values = column[~na_mask].values.copy()  # Create a copy to avoid modifying the original
        non_na_count = len(non_na_values)
        
        self.logger.debug(f"Shuffling {non_na_count} non-null values, preserving {na_mask.sum()} null positions")
        
        # Use random.shuffle for in-place shuffling
        random.shuffle(non_na_values)
        
        # Create a new series with shuffled values
        shuffled = pd.Series(index=column.index, dtype=column.dtype)
        shuffled[~na_mask] = non_na_values
        shuffled[na_mask] = None
        
        # Log statistics
        self._log_stats(column, shuffled, rule)
        
        self.logger.debug(f"Successfully shuffled values")
        return shuffled
# forge_engine/strategies/passthrough.py
"""
Passthrough masking strategy for the forge_engine package.
Keeps the original values unchanged.
"""

import pandas as pd
from typing import Dict, Any, Optional

from forge_engine.strategies.base import BaseMaskingStrategy


class PassthroughStrategy(BaseMaskingStrategy):
    """
    Masking strategy that keeps the original values unchanged.
    Useful for non-sensitive data or fields needed for reference.
    """
    
    def apply(self, column: pd.Series, rule: Dict[str, Any]) -> pd.Series:
        """
        Keeps the original values unchanged
        
        Args:
            column: Pandas Series to mask
            rule: Dictionary containing the masking rule configuration
            
        Returns:
            The original pandas Series unchanged
        """
        self.logger.debug(f"Applying passthrough mask (no changes)")
        
        # Simply return the original column
        return column
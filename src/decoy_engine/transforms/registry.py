# decoy_engine/strategies/manager.py
"""
Strategy manager for coordinating masking strategies in the decoy_engine package.
"""

import pandas as pd
from typing import Dict, Any, Optional, List

from decoy_engine.transforms.base import BaseMaskingStrategy
from decoy_engine.transforms.factory import create_strategy


class StrategyManager:
    """
    Manages the creation and application of masking strategies.
    Provides a centralized way to apply masking rules to columns.
    """
    
    def __init__(self, seed: int = 42, logger=None, derive_key=None):
        """
        Initialize the strategy manager

        Args:
            seed: Global random seed for deterministic masking (legacy fallback)
            logger: Logger instance (optional)
            derive_key: Optional ``(info: str) -> bytes`` for keyed
                determinism. Forwarded to every strategy this manager builds.
        """
        self.seed = seed
        self.derive_key = derive_key

        # Use provided logger or create a default one
        if logger:
            self.logger = logger
        else:
            from decoy_engine.internal.logging import get_logger
            self.logger = get_logger()

        # Cache for strategy instances
        self._strategy_cache = {}

        keyed = "yes" if derive_key is not None else "no"
        self.logger.debug(f"Initialized StrategyManager with seed: {seed} (keyed: {keyed})")
    
    def get_strategy(self, strategy_type: str) -> BaseMaskingStrategy:
        """
        Get or create a strategy instance for the specified type
        
        Args:
            strategy_type: Type of masking strategy
            
        Returns:
            Masking strategy instance
        """
        # Check cache first
        if strategy_type in self._strategy_cache:
            return self._strategy_cache[strategy_type]
        
        # Create new strategy
        strategy = create_strategy(
            strategy_type, self.seed, self.logger, derive_key=self.derive_key
        )

        # Cache it
        self._strategy_cache[strategy_type] = strategy

        return strategy
    
    def apply_masking_rule(self, column: pd.Series, rule: Dict[str, Any]) -> pd.Series:
        """
        Apply a masking rule to a column
        
        Args:
            column: Pandas Series to mask
            rule: Dictionary containing the masking rule configuration
            
        Returns:
            Masked pandas Series
        """
        strategy_type = rule.get('type', 'passthrough')
        
        if not strategy_type:
            self.logger.warning(f"No strategy type specified for column '{rule.get('column')}', using passthrough")
            strategy_type = 'passthrough'
        
        # Get appropriate strategy
        strategy = self.get_strategy(strategy_type)
        
        # Validate rule
        strategy.validate_rule(rule)
        
        # Apply the strategy
        return strategy.apply(column, rule)
    
    def apply_masking_rules(self, df: pd.DataFrame, rules: List[Dict[str, Any]]) -> pd.DataFrame:
        """
        Apply multiple masking rules to a DataFrame
        
        Args:
            df: Pandas DataFrame to mask
            rules: List of masking rule dictionaries
            
        Returns:
            Masked pandas DataFrame
        """
        result = df.copy()
        
        # Apply each rule
        for rule in rules:
            column_name = rule.get('column')
            
            # Skip if column doesn't exist
            if column_name not in df.columns:
                self.logger.warning(f"Column '{column_name}' not found in DataFrame. Skipping.")
                continue
            
            # Apply the rule
            self.logger.info(f"Applying masking rule to column '{column_name}' with type '{rule.get('type')}'")
            result[column_name] = self.apply_masking_rule(df[column_name], rule)
        
        return result
    
    def available_strategies(self) -> List[str]:
        """
        Get a list of available masking strategy types
        
        Returns:
            List of strategy type names
        """
        from decoy_engine.transforms import (
            CategoricalStrategy, FakerStrategy, HashStrategy, RedactStrategy,
            ShuffleStrategy, PassthroughStrategy
        )
        
        # Get all strategy classes from the strategies module
        strategies = [
            CategoricalStrategy, FakerStrategy, HashStrategy, RedactStrategy,
            ShuffleStrategy, PassthroughStrategy
        ]
        
        # Extract strategy names
        return [strategy().strategy_name for strategy in strategies]
    
    def get_strategy_info(self, strategy_type: str) -> Dict[str, Any]:
        """
        Get information about a specific strategy
        
        Args:
            strategy_type: Type of masking strategy
            
        Returns:
            Dictionary with strategy information
        """
        # Create strategy to get its information
        strategy = self.get_strategy(strategy_type)
        
        # Get strategy class documentation
        doc = strategy.__class__.__doc__ or ""
        
        # Basic info
        info = {
            'name': strategy.strategy_name,
            'description': doc.strip(),
            'required_parameters': ['column', 'type'],
            'optional_parameters': ['seed']
        }
        
        # Add strategy-specific parameters
        if strategy_type == 'faker':
            info['optional_parameters'].extend(['faker_type', 'preserve_domain'])
            info['example'] = {
                'column': 'example_column',
                'type': 'faker',
                'faker_type': 'name'
            }
        elif strategy_type == 'hash':
            info['example'] = {
                'column': 'example_column',
                'type': 'hash'
            }
        elif strategy_type == 'redact':
            info['optional_parameters'].append('redact_with')
            info['example'] = {
                'column': 'example_column',
                'type': 'redact',
                'redact_with': 'REDACTED'
            }
        elif strategy_type == 'categorical':
            info['required_parameters'].append('categories')
            info['optional_parameters'].extend(['weights', 'null_probability'])
            info['example'] = {
                'column': 'example_column',
                'type': 'categorical',
                'categories': ['active', 'inactive', 'pending'],
                'weights': [7, 2, 1],
            }
        elif strategy_type == 'shuffle':
            info['example'] = {
                'column': 'example_column',
                'type': 'shuffle'
            }
        elif strategy_type == 'passthrough':
            info['example'] = {
                'column': 'example_column',
                'type': 'passthrough'
            }
        
        return info

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

        # Log only the operating mode the strategies will actually use:
        # in keyed mode the seed is a dormant legacy fallback (covered
        # by faker_based / categorical / fpe), so it's noise in the
        # log; in seeded mode the seed value is what drives output.
        if derive_key is not None:
            self.logger.debug("Initialized StrategyManager (mode: keyed)")
        else:
            self.logger.debug(
                f"Initialized StrategyManager (mode: seeded, seed: {seed})",
            )
    
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
            new_col = self.apply_masking_rule(df[column_name], rule)

            # Drop the original column's extension-dtype tag (e.g.
            # int64[pyarrow] for a date column that the CSV reader
            # inferred as int because values looked like 20260522)
            # when the masked output is a different semantic type.
            # Without this, pa.Table.from_pandas at the op boundary
            # (decoy_engine.graph.conversion.engine_to_arrow line 98)
            # honors the original int64 tag and tries to coerce the
            # now-string masked values back to int, raising the
            # opaque "object of type <class 'str'> cannot be
            # converted to int" / "Conversion failed for column X"
            # tuple. Re-instantiating result[column_name] from a
            # fresh Series whose dtype matches the masked output
            # (object for the str-typed strategies) lets Arrow
            # infer cleanly on the way out.
            original_dtype = df[column_name].dtype
            if (
                pd.api.types.is_extension_array_dtype(original_dtype)
                and not pd.api.types.is_extension_array_dtype(new_col.dtype)
            ):
                # Strategy returned a numpy-backed Series (object dtype
                # for date_shift / hash / faker / redact / etc.). Pandas
                # would otherwise try to coerce the new values to fit
                # the existing extension dtype tag at assignment time;
                # for int64[pyarrow] columns receiving string masked
                # values, that raises the opaque "object of type
                # <class 'str'> cannot be converted to int" tuple.
                # Drop + reinsert at the same column index so order is
                # preserved (a straight reassign appends to the end).
                col_idx = result.columns.get_loc(column_name)
                result = result.drop(columns=[column_name])
                result.insert(col_idx, column_name, new_col)
                continue

            result[column_name] = new_col

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

# decoy_engine/strategies/map.py
"""
Map masking strategy for the decoy_engine package.
Uses persistent mapping dictionaries for consistent replacements.
"""

import pandas as pd
import random
from typing import Dict, Any, Optional

from decoy_engine.transforms.base import BaseMaskingStrategy
from decoy_engine.internal.helpers import (
    deterministic_hash,
    get_faker_providers,
    make_faker,
)


class MapStrategy(BaseMaskingStrategy):
    """
    Masking strategy that uses mapping dictionaries for consistent replacements.
    Stores mappings persistently to ensure consistency across runs.
    """
    
    def __init__(self, seed: int = 42, logger=None):
        """
        Initialize the map strategy with seed for deterministic behavior
        
        Args:
            seed: Random seed for deterministic masking
            logger: Logger instance (optional)
        """
        super().__init__(seed, logger)
        random.seed(self.seed)
    
    def apply(self, column: pd.Series, rule: Dict[str, Any]) -> pd.Series:
        """
        Uses persistent mapping dictionaries to ensure consistent replacements across runs
        
        Args:
            column: Pandas Series to mask
            rule: Dictionary containing the masking rule configuration
            
        Returns:
            Pandas Series with mapped values
        """
        column_name = rule['column']
        
        # Get mapping manager
        from decoy_engine.internal.mappings import MappingManager
        mapping_manager = MappingManager(logger=self.logger)
        
        # Load existing mapping
        mapping = mapping_manager.load_mapping(column_name)
        
        # Get map type and related configuration
        map_type = rule.get('map_type', 'faker')
        seed = rule.get('seed', self.seed)
        faker_type = rule.get('faker_type', 'word')
        
        self.logger.debug(f"Map configuration: type='{map_type}', seed={seed}, faker_type='{faker_type}'")
        
        # Process each unique value
        updated = False
        unique_values = column.dropna().unique()
        unique_count = len(unique_values)
        
        self.logger.debug(f"Processing {unique_count} unique values for column '{column_name}'")
        
        # Count values needing mapping
        new_values = [str(v) for v in unique_values if str(v) not in mapping]
        new_count = len(new_values)
        
        if new_count > 0:
            self.logger.info(f"Adding {new_count} new values to mapping for column '{column_name}'")
            updated = True
            
            # Generate mapped values based on map_type
            if map_type == 'faker':
                # Create faker with seed; respect optional locale override.
                fake = make_faker(rule.get('locale'))
                fake.seed_instance(seed)
                faker_providers = get_faker_providers(fake)
                
                for value in unique_values:
                    # Skip if already mapped
                    str_value = str(value)
                    if str_value in mapping:
                        continue
                    
                    # Special handling for email with preserve_domain
                    if faker_type == 'email' and rule.get('preserve_domain', False) and '@' in str(value):
                        username, domain = str(value).split('@', 1)
                        mapping[str_value] = f"{fake.user_name()}@{domain}"
                        self.logger.debug(f"Preserving domain for email: {domain}")
                        continue
                    
                    # Use faker provider from our dictionary if available
                    if faker_type in faker_providers:
                        mapping[str_value] = faker_providers[faker_type]()
                    else:
                        # Default to a word if faker_type is not recognized
                        self.logger.warning(f"Unknown faker_type '{faker_type}', using 'word' instead")
                        mapping[str_value] = faker_providers['word']()
                
            elif map_type == 'hash':
                # Use deterministic hash for mapping
                for value in unique_values:
                    str_value = str(value)
                    if str_value in mapping:
                        continue
                    
                    mapping[str_value] = deterministic_hash(str_value, seed)
                
            elif map_type == 'fixed':
                # Use a fixed value with numeric suffix for uniqueness
                prefix = rule.get('fixed_prefix', 'MASKED')
                
                for value in unique_values:
                    str_value = str(value)
                    if str_value in mapping:
                        continue
                    
                    mapping[str_value] = f"{prefix}_{len(mapping) + 1}"
            
            elif map_type == 'manual':
                explicit = rule.get('mapping', {})
                for value in unique_values:
                    str_value = str(value)
                    if str_value in mapping:
                        continue
                    mapping[str_value] = explicit.get(str_value, str_value)

            else:
                self.logger.warning(f"Unknown map_type '{map_type}', using 'hash' instead")
                
                # Fall back to hash if unknown map type
                for value in unique_values:
                    str_value = str(value)
                    if str_value in mapping:
                        continue
                    
                    mapping[str_value] = deterministic_hash(str_value, seed)
        
        # Save mapping if updated
        if updated:
            self.logger.info(f"Updated mapping for column '{column_name}' with {len(mapping)} entries")
            mapping_manager.save_mapping(mapping, column_name)
        
        # Apply mapping to column
        def map_value(val):
            if val is None or pd.isna(val):
                return val
            return mapping.get(str(val), val)
        
        result = column.apply(map_value)
        
        # Log statistics
        self._log_stats(column, result, rule)
        
        return result
    
    def validate_rule(self, rule: Dict[str, Any]) -> None:
        """
        Validate that the rule contains all required fields for the map strategy
        
        Args:
            rule: Dictionary containing the masking rule configuration
            
        Raises:
            ValueError: If rule validation fails
        """
        super().validate_rule(rule)
        
        # Set default map_type if not specified
        if 'map_type' not in rule:
            rule['map_type'] = 'faker'
            self.logger.debug(f"Using default map_type: 'faker' for column '{rule['column']}'")
        
        # Check for faker_type if map_type is 'faker'
        if rule['map_type'] == 'faker' and 'faker_type' not in rule:
            rule['faker_type'] = 'word'
            self.logger.debug(f"Using default faker_type: 'word' for column '{rule['column']}'")
        
        # Check for fixed_prefix if map_type is 'fixed'
        if rule['map_type'] == 'fixed' and 'fixed_prefix' not in rule:
            rule['fixed_prefix'] = 'MASKED'
            self.logger.debug(f"Using default fixed_prefix: 'MASKED' for column '{rule['column']}'")
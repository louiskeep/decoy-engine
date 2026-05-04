# forge_engine/strategies/faker.py
"""
Faker masking strategy for the forge_engine package.
Replaces values with realistic fake data using the Faker library.
"""

import pandas as pd
import random
from typing import Dict, Any, Optional
from faker import Faker

from forge_engine.transforms.base import BaseMaskingStrategy
from forge_engine.internal.helpers import get_faker_providers


class FakerStrategy(BaseMaskingStrategy):
    """
    Masking strategy that replaces values with realistic fake data.
    Uses the Faker library to generate fake values that look realistic.
    """
    
    def __init__(self, seed: int = 42, logger=None):
        """
        Initialize the faker strategy with seed for deterministic behavior
        
        Args:
            seed: Random seed for deterministic masking
            logger: Logger instance (optional)
        """
        super().__init__(seed, logger)
        # Set random seed for consistent masking
        random.seed(self.seed)
    
    def apply(self, column: pd.Series, rule: Dict[str, Any]) -> pd.Series:
        """
        Replace values with realistic fake data that's consistent for identical inputs
        
        Args:
            column: Pandas Series to mask
            rule: Dictionary containing the masking rule configuration
            
        Returns:
            Pandas Series with masked values
        """
        faker_type = rule.get('faker_type', 'word')
        
        # Create a deterministic faker with a seed if specified
        rule_seed = rule.get('seed', self.seed)  # Default to global seed for consistency
        deterministic_faker = Faker()
        deterministic_faker.seed_instance(rule_seed)
        
        self.logger.debug(f"Applying faker mask with type '{faker_type}' and seed {rule_seed}")
        
        # Get all available faker providers
        faker_providers = get_faker_providers(deterministic_faker)
        
        # Create a mapping for unique values to ensure consistency
        unique_values = column.unique()
        unique_non_null = [v for v in unique_values if v is not None and not pd.isna(v)]
        self.logger.debug(f"Processing {len(unique_non_null)} unique non-null values")
        
        faker_map = {}
        
        for value in unique_values:
            if value is None or pd.isna(value):
                faker_map[value] = value  # Preserve None/NaN values
                continue
                
            # Special handling for email with preserve_domain
            if faker_type == 'email' and rule.get('preserve_domain', False) and '@' in str(value):
                username, domain = str(value).split('@', 1)
                faker_map[value] = f"{deterministic_faker.user_name()}@{domain}"
                self.logger.debug(f"Preserving domain for email: {domain}")
                continue
            
            # Use faker provider from our dictionary if available
            if faker_type in faker_providers:
                faker_map[value] = faker_providers[faker_type]()
            else:
                # Default to a word if faker_type is not recognized
                self.logger.warning(f"Unknown faker_type '{faker_type}', using 'word' instead")
                faker_map[value] = faker_providers['word']()
        
        # Apply the mapping to the entire column
        result = column.map(faker_map)
        
        # Log statistics
        self._log_stats(column, result, rule)
        
        return result
    
    def validate_rule(self, rule: Dict[str, Any]) -> None:
        """
        Validate that the rule contains all required fields for the faker strategy
        
        Args:
            rule: Dictionary containing the masking rule configuration
            
        Raises:
            ValueError: If rule validation fails
        """
        super().validate_rule(rule)
        
        # Faker-specific validation
        if 'faker_type' not in rule:
            # Default to 'word' if not specified
            rule['faker_type'] = 'word'
            self.logger.debug(f"Using default faker_type: 'word' for column '{rule['column']}'")
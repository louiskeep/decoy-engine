# decoy_engine/core/integrity.py
"""
Referential integrity management for the decoy_engine package.
Ensures consistent masking across related columns and tables.
"""

import pandas as pd
import hashlib
from typing import Dict, Any, List, Optional, Set, Tuple

class ReferentialIntegrityManager:
    """
    Manages referential integrity for data masking.
    Ensures that related data elements are masked consistently.
    """
    
    def __init__(self, config: Dict[str, Any], logger=None):
        """
        Initialize with configuration and mapping utilities
        
        Args:
            config: Dictionary with configuration
            logger: Logger instance (optional)
        """
        self.config = config
        
        # Use provided logger or create a default one
        if logger:
            self.logger = logger
        else:
            from decoy_engine.internal.logging import get_logger
            self.logger = get_logger()
        
        # Initialize mapping storage
        from decoy_engine.internal.mappings import MappingManager
        mappings_dir = self.config.get('mappings', {}).get('store_directory', 'mappings/')
        self.mapping_manager = MappingManager(mappings_dir=mappings_dir, logger=self.logger)
        
        # Initialize internal cache
        self.global_mapping_cache = {}
        
        # Load existing global mappings
        if 'referential_integrity' in self.config:
            self._load_global_mappings()
            
            # Log relationships if defined
            rel_count = len(self.config['referential_integrity'])
            self.logger.info(f"Initialized with {rel_count} referential integrity relationships")
            for rel in self.config['referential_integrity']:
                rel_name = rel['name']
                col_count = len(rel.get('columns', []))
                self.logger.debug(f"Relationship '{rel_name}' includes {col_count} columns")
                for col in rel.get('columns', []):
                    self.logger.debug(f"  - {col}")
        else:
            self.logger.info("No referential integrity relationships defined")
    
    def _load_global_mappings(self) -> None:
        """
        Load global mappings for referential integrity from storage
        """
        for relationship in self.config.get('referential_integrity', []):
            rel_name = relationship['name']
            self.global_mapping_cache[rel_name] = self.mapping_manager.load_global_mapping(rel_name)
            self.logger.info(f"Loaded mapping for relationship '{rel_name}' with {len(self.global_mapping_cache[rel_name])} entries")
    
    def save_global_mappings(self) -> None:
        """
        Save global mappings to persistent storage
        """
        for rel_name, mapping in self.global_mapping_cache.items():
            self.mapping_manager.save_global_mapping(mapping, rel_name)
            self.logger.info(f"Saved mapping for relationship '{rel_name}' with {len(mapping)} entries")
    
    def get_referential_relationship(self, table_name: str, column_name: str) -> Optional[str]:
        """
        Determine if a column is part of a referential integrity relationship
        
        Args:
            table_name: Name of the table
            column_name: Name of the column
            
        Returns:
            Relationship name or None if not part of a relationship
        """
        # Skip if no referential integrity section
        if 'referential_integrity' not in self.config:
            return None
            
        column_ref = f"{table_name}.{column_name}"
        
        # For debugging
        self.logger.debug(f"Checking if column '{column_ref}' is part of any relationships")
        
        # Try with exact match first
        for relationship in self.config['referential_integrity']:
            if column_ref in relationship.get('columns', []):
                self.logger.debug(f"Found exact match in relationship '{relationship['name']}'")
                return relationship['name']
        
        # If no exact match, try a case-insensitive match
        for relationship in self.config['referential_integrity']:
            columns = relationship.get('columns', [])
            for col in columns:
                if column_ref.lower() == col.lower():
                    self.logger.debug(f"Found case-insensitive match in relationship '{relationship['name']}'")
                    return relationship['name']
                    
        self.logger.debug(f"No relationship found for column '{column_ref}'")
        return None
    
    def apply_global_mapping(self, column: pd.Series, rel_name: str, rule: Dict[str, Any]) -> pd.Series:
        """
        Apply masking using a global mapping to maintain referential integrity
        
        Args:
            column: Pandas Series to mask
            rel_name: Name of referential integrity relationship
            rule: Dictionary containing the masking rule configuration
            
        Returns:
            Pandas Series with masked values
        """
        # Ensure we have a mapping dictionary for this relationship
        if rel_name not in self.global_mapping_cache:
            self.global_mapping_cache[rel_name] = {}
        
        mapping = self.global_mapping_cache[rel_name]
        updated = False
        
        # Get mask type and column name
        mask_type = rule['type']
        column_name = rule['column']
        
        # Process each unique value
        unique_values = column.dropna().unique()
        self.logger.info(f"Applying global mapping for relationship '{rel_name}', column '{column_name}' with mask type '{mask_type}'")
        
        # Process new values
        new_values = [str(value) for value in unique_values if str(value) not in mapping]
        if new_values:
            self.logger.info(f"Adding {len(new_values)} new values to global mapping '{rel_name}'")
            updated = True
            
            # Create appropriate strategy for this mask type
            from decoy_engine.transforms.factory import create_strategy
            strategy = create_strategy(mask_type, rule.get('seed', 0), self.logger)
            
            # Process each new value
            for value in new_values:
                if value is None:
                    continue
                    
                str_value = str(value)
                if str_value in mapping:
                    continue
                
                # Generate a deterministic seed for this value
                value_seed = int(hashlib.md5(f"{str_value}{rel_name}".encode()).hexdigest(), 16) % (2**32)
                
                # Create a single-value Series for the strategy to process
                single_value_series = pd.Series([value])
                
                # Apply the strategy with a custom seed
                modified_rule = rule.copy()
                modified_rule['seed'] = value_seed
                
                # Apply the strategy and get the first (only) value
                masked_value = strategy.apply(single_value_series, modified_rule).iloc[0]
                
                # Store in mapping
                mapping[str_value] = masked_value
        
        # Apply mapping to column
        def map_value(val):
            if val is None or pd.isna(val):
                return val
            return mapping.get(str(val), val)
        
        result = column.apply(map_value)
        self.logger.debug(f"Applied mapping to {len(column)} values")
        
        return result

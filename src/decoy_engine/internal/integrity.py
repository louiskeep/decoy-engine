# decoy_engine/core/integrity.py
"""
Referential integrity management for the decoy_engine package.
Ensures consistent masking across related columns and tables.
"""

import pandas as pd
from typing import Dict, Any, Optional


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
        
        # V1 policy: no local mapping store. Referential integrity is enforced
        # by deterministic transforms, not by persisted mapping files.
        self.global_mapping_cache = {}
        
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
        Initialize relationship names without loading local mapping files.
        """
        for relationship in self.config.get('referential_integrity', []):
            rel_name = relationship['name']
            self.global_mapping_cache[rel_name] = {}
            self.logger.debug(
                f"Initialized deterministic relationship '{rel_name}' without mapping storage"
            )
    
    def save_global_mappings(self) -> None:
        """
        No-op retained for older callers. V1 does not persist mapping files.
        """
        self.logger.debug("Skipping global mapping persistence; local mapping stores are disabled")
    
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
        # Get mask type and column name
        mask_type = rule['type']
        column_name = rule['column']

        self.logger.info(
            f"Applying deterministic relationship '{rel_name}', column "
            f"'{column_name}' with mask type '{mask_type}'"
        )

        from decoy_engine.transforms.factory import create_strategy
        strategy = create_strategy(mask_type, rule.get('seed', 0), self.logger)
        relationship_rule = rule.copy()
        relationship_rule['column'] = rel_name
        result = strategy.apply(column, relationship_rule)
        self.logger.debug(f"Applied deterministic relationship transform to {len(column)} values")
        
        return result

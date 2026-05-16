# decoy_engine/utils/mappings.py
"""
Mapping utilities for categorical remap persistence.
"""

import json
import os
from pathlib import Path
from typing import Dict, Any, Optional


class MappingManager:
    """
    Handles operations related to mapping storage and retrieval.

    Construction is side-effect free. The directory is created only when a
    caller explicitly saves a categorical mapping.
    """
    
    def __init__(self, mappings_dir: str = "mappings/", logger=None):
        """
        Initialize with a mappings directory
        
        Args:
            mappings_dir: Directory to store categorical mapping files
            logger: Logger instance
        """
        self.mappings_dir = mappings_dir
        
        # Use provided logger or create a default one
        if logger:
            self.logger = logger
        else:
            from decoy_engine.internal.logging import get_logger
            self.logger = get_logger()
            
        self.logger.debug(f"Initialized MappingManager with directory: {mappings_dir}")
        
        # Internal cache for performance
        self._mapping_cache = {}

    def _is_categorical_method(self, method: Optional[str]) -> bool:
        return str(method or "").lower() == "categorical"
    
    def get_mapping_path(self, column: str) -> str:
        """
        Generates a standardized file path for storing categorical mappings.
        
        Args:
            column: Column name to create mapping for
            
        Returns:
            Path to the categorical mapping JSON file
        """
        # Create a sanitized filename
        safe_name = column.replace(' ', '_').lower()
        mapping_path = os.path.join(self.mappings_dir, f"{safe_name}_map.json")
        self.logger.debug(f"Mapping path for column '{column}': {mapping_path}")
        return mapping_path
    
    def get_global_mapping_path(self, relationship_name: str) -> str:
        """
        Generates a file path for storing global mapping dictionaries for referential integrity.
        
        Args:
            relationship_name: Name of the referential integrity relationship
            
        Returns:
            Path to the categorical global mapping JSON file
        """
        # Create a sanitized filename
        safe_name = relationship_name.replace(' ', '_').lower()
        mapping_path = os.path.join(self.mappings_dir, f"global_{safe_name}_map.json")
        self.logger.debug(f"Global mapping path for relationship '{relationship_name}': {mapping_path}")
        return mapping_path
    
    def load_mapping(self, column: str, method: Optional[str] = None) -> Dict[str, Any]:
        """
        Load a categorical mapping dictionary for a specific column.
        
        Args:
            column: Column name
            
        Returns:
            Dictionary with original values as keys and masked values as values
        """
        if not self._is_categorical_method(method):
            self.logger.debug(
                f"Skipping mapping load for column '{column}'; method is not categorical"
            )
            return {}

        # Check cache first
        if column in self._mapping_cache:
            return self._mapping_cache[column]
        
        # Load from file
        mapping_path = self.get_mapping_path(column)
        
        if not os.path.exists(mapping_path):
            self._mapping_cache[column] = {}
            return {}
        
        try:
            with open(mapping_path, 'r') as f:
                mapping = json.load(f)
                
            self._mapping_cache[column] = mapping
            self.logger.debug(f"Loaded mapping for column '{column}' with {len(mapping)} entries")
            return mapping
        except Exception as e:
            self.logger.warning(f"Error loading mapping for column '{column}': {str(e)}")
            self._mapping_cache[column] = {}
            return {}
    
    def save_mapping(
        self,
        mapping: Dict[str, Any],
        column: str,
        method: Optional[str] = None,
    ) -> None:
        """
        Save a categorical mapping dictionary for a specific column.
        
        Args:
            mapping: Dictionary with original values as keys and masked values as values
            column: Column name
        """
        if not self._is_categorical_method(method):
            self.logger.debug(
                f"Skipping mapping save for column '{column}'; method is not categorical"
            )
            return

        # Update cache
        self._mapping_cache[column] = mapping
        
        # Save to file
        mapping_path = self.get_mapping_path(column)
        
        # Ensure directory exists
        Path(os.path.dirname(mapping_path)).mkdir(parents=True, exist_ok=True)
        
        try:
            with open(mapping_path, 'w') as f:
                json.dump(mapping, f, indent=4)
                
            self.logger.debug(f"Saved mapping for column '{column}' with {len(mapping)} entries")
        except Exception as e:
            self.logger.error(f"Error saving mapping for column '{column}': {str(e)}")
    
    def load_global_mapping(
        self,
        relationship_name: str,
        method: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Load a categorical global mapping dictionary for referential integrity.
        
        Args:
            relationship_name: Name of the referential integrity relationship
            
        Returns:
            Dictionary with original values as keys and masked values as values
        """
        if not self._is_categorical_method(method):
            self.logger.debug(
                f"Skipping global mapping load for relationship '{relationship_name}'; "
                "method is not categorical"
            )
            return {}

        mapping_path = self.get_global_mapping_path(relationship_name)
        
        if not os.path.exists(mapping_path):
            return {}
        
        try:
            with open(mapping_path, 'r') as f:
                mapping = json.load(f)
                
            self.logger.debug(f"Loaded global mapping for relationship '{relationship_name}' with {len(mapping)} entries")
            return mapping
        except Exception as e:
            self.logger.warning(f"Error loading global mapping for relationship '{relationship_name}': {str(e)}")
            return {}
    
    def save_global_mapping(
        self,
        mapping: Dict[str, Any],
        relationship_name: str,
        method: Optional[str] = None,
    ) -> None:
        """
        Save a categorical global mapping dictionary for referential integrity.
        
        Args:
            mapping: Dictionary with original values as keys and masked values as values
            relationship_name: Name of the referential integrity relationship
        """
        if not self._is_categorical_method(method):
            self.logger.debug(
                f"Skipping global mapping save for relationship '{relationship_name}'; "
                "method is not categorical"
            )
            return

        mapping_path = self.get_global_mapping_path(relationship_name)
        
        # Ensure directory exists
        Path(os.path.dirname(mapping_path)).mkdir(parents=True, exist_ok=True)
        
        try:
            with open(mapping_path, 'w') as f:
                json.dump(mapping, f, indent=4)
                
            self.logger.debug(f"Saved global mapping for relationship '{relationship_name}' with {len(mapping)} entries")
        except Exception as e:
            self.logger.error(f"Error saving global mapping for relationship '{relationship_name}': {str(e)}")

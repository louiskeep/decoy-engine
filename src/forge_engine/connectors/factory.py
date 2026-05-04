# forge_engine/io/factory.py
"""
Factory pattern for creating I/O handlers based on configuration.
"""

from typing import Dict, Any, Optional

from forge_engine.connectors.base import IOHandler


def create_io_handler(input_config: Dict[str, Any], output_config: Dict[str, Any], config: Dict[str, Any] = None, logger=None) -> IOHandler:
    """
    Factory function to create appropriate I/O handler based on configuration
    
    Args:
        input_config: Dictionary with input configuration
        output_config: Dictionary with output configuration
        config: Full configuration dictionary (optional)
        logger: Logger instance (optional)
        
    Returns:
        Appropriate I/O handler instance
        
    Raises:
        ValueError: If input type is not supported
    """
    input_type = input_config.get('type', 'csv').lower()
    
    if input_type == 'csv':
        from forge_engine.connectors.csv_connector import CSVHandler
        return CSVHandler(input_config, output_config, logger)
    elif input_type == 'fixed_width':
        from forge_engine.connectors.fixed_width import FixedWidthHandler
        return FixedWidthHandler(input_config, output_config, config, logger)
    elif input_type == 'database':
        from forge_engine.connectors.database import DBHandler
        return DBHandler(input_config, output_config, logger)
    else:
        raise ValueError(f"Unsupported input type: {input_type}")
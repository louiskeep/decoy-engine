# forge_engine/utils/logging.py
"""
Logging utilities for the forge_engine package.
"""

import logging
import os
from pathlib import Path
from logging.handlers import RotatingFileHandler
from typing import Dict, Any, Optional

# Create a module-level logger instance
_LOGGER_INSTANCE = None


def get_logger(config: Optional[Dict[str, Any]] = None):
    """
    Get or create a logger instance (singleton pattern)
    
    Args:
        config: Dictionary with logging configuration
        
    Returns:
        Logger instance
    """
    global _LOGGER_INSTANCE
    if _LOGGER_INSTANCE is None:
        _LOGGER_INSTANCE = _create_logger(config)
    elif config:
        # Reconfigure existing logger if new config provided
        _configure_logger(_LOGGER_INSTANCE, config)
        
    return _LOGGER_INSTANCE


def _create_logger(config: Optional[Dict[str, Any]] = None):
    """
    Create a new logger instance
    
    Args:
        config: Dictionary with logging configuration
        
    Returns:
        Logger instance
    """
    # Set defaults if no config provided
    if config is None:
        config = {}
        
    logger = logging.getLogger('forge_engine')
    _configure_logger(logger, config)
    return logger


def _configure_logger(logger, config: Dict[str, Any]):
    """
    Configure the logger with the provided settings
    
    Args:
        logger: Logger instance to configure
        config: Dictionary with logging configuration
    """
    # Clear any existing handlers to avoid duplicates
    if logger.handlers:
        logger.handlers.clear()
        
    # Set default level
    level_name = config.get('level', 'info').lower()
    level_map = {
        'debug': logging.DEBUG,
        'info': logging.INFO,
        'warning': logging.WARNING,
        'error': logging.ERROR,
        'critical': logging.CRITICAL
    }
    level = level_map.get(level_name, logging.INFO)
    logger.setLevel(level)
    
    # Create formatters
    default_format = '%(asctime)s - %(levelname)s - %(message)s'
    verbose_format = '%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s'
    
    fmt = verbose_format if config.get('verbose', False) else default_format
    formatter = logging.Formatter(fmt, datefmt='%Y-%m-%d %H:%M:%S')
    
    # Default to no console output (console: False by default)
    if config.get('console', False):
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
    
    # File handler - default to a log file if not specified
    log_file = config.get('file', 'logs/forge_engine.log')
    
    # Create directory if it doesn't exist
    log_dir = os.path.dirname(log_file)
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        
    # Set up rotating file handler with max size and backup count
    max_size_mb = config.get('max_size_mb', 10)
    backup_count = config.get('backup_count', 5)
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=max_size_mb * 1024 * 1024,
        backupCount=backup_count
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
        
    # Only log this to file, not to console
    logger.info("Logging initialized")


class ProgressLogger:
    """
    Helper class for logging progress with percentage and ETA.
    """
    
    def __init__(self, logger, total: int, message: str = "Progress"):
        """
        Initialize with logger and total items
        
        Args:
            logger: Logger instance
            total: Total number of items to process
            message: Description of the progress
        """
        self.logger = logger
        self.total = total
        self.message = message
        self.current = 0
        self.start_time = None
    
    def start(self):
        """
        Start the progress tracking
        """
        import time
        self.start_time = time.time()
        self.logger.info(f"{self.message}: Starting processing of {self.total:,} items")
    
    def update(self, increment: int = 1):
        """
        Update progress
        
        Args:
            increment: Number of items processed since last update
        """
        import time
        self.current += increment
        
        if self.total > 0:
            percentage = (self.current / self.total) * 100
            elapsed_time = time.time() - self.start_time if self.start_time else 0
            
            # Calculate speed and ETA
            if elapsed_time > 0:
                speed = self.current / elapsed_time
                remaining_items = self.total - self.current
                if speed > 0:
                    eta_seconds = remaining_items / speed
                    
                    # Format ETA nicely
                    if eta_seconds < 60:
                        eta_str = f"{eta_seconds:.0f} seconds"
                    elif eta_seconds < 3600:
                        eta_str = f"{eta_seconds/60:.1f} minutes"
                    else:
                        eta_str = f"{eta_seconds/3600:.1f} hours"
                        
                    # Format speed appropriately based on magnitude
                    if speed < 1:
                        speed_str = f"{speed:.2f} items/sec"
                    elif speed < 10:
                        speed_str = f"{speed:.1f} items/sec"
                    else:
                        speed_str = f"{speed:.0f} items/sec"
                    
                    self.logger.info(f"{self.message}: {percentage:.1f}% complete ({self.current:,}/{self.total:,}) - {speed_str} - ETA: {eta_str}")
                else:
                    self.logger.info(f"{self.message}: {percentage:.1f}% complete ({self.current:,}/{self.total:,})")
            else:
                self.logger.info(f"{self.message}: {percentage:.1f}% complete ({self.current:,}/{self.total:,})")
        else:
            # If total is unknown, just log current progress
            self.logger.info(f"{self.message}: {self.current:,} items processed")
    
    def finish(self):
        """
        Mark the progress as complete
        """
        import time
        elapsed_time = time.time() - self.start_time if self.start_time else 0
        
        if elapsed_time > 0:
            speed = self.current / elapsed_time
            if speed < 1:
                speed_str = f"{speed:.2f} items/sec"
            elif speed < 10:
                speed_str = f"{speed:.1f} items/sec"
            else:
                speed_str = f"{speed:.0f} items/sec"
                
            self.logger.info(f"{self.message}: Completed processing {self.current:,} items in {elapsed_time:.1f} seconds ({speed_str})")
        else:
            self.logger.info(f"{self.message}: Completed processing {self.current:,} items")
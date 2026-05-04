# forge_engine/utils/memory.py
"""
Memory monitoring utilities for the forge_engine package.
"""

from typing import Optional, Dict, Any, Tuple


class MemoryMonitor:
    """
    Utility class for monitoring memory usage during processing.
    """
    
    @staticmethod
    def monitor_memory_usage(logger, label: str = "Current") -> Optional[float]:
        """
        Monitor and report current memory usage
        
        Args:
            logger: Logger instance to use for reporting
            label: Label for the memory usage report
            
        Returns:
            Memory usage in MB or None if monitoring failed
        """
        try:
            import psutil
            import os
            
            # Get the current process
            process = psutil.Process(os.getpid())
            
            # Get memory info
            memory_info = process.memory_info()
            
            # Calculate memory usage in MB
            memory_usage_mb = memory_info.rss / (1024 * 1024)
            
            logger.info(f"Memory Usage ({label}): {memory_usage_mb:.2f} MB")
            
            # Get system memory info for context
            system_memory = psutil.virtual_memory()
            system_total_gb = system_memory.total / (1024**3)
            system_used_percent = system_memory.percent
            
            logger.debug(f"System Memory: {system_used_percent}% of {system_total_gb:.1f} GB used")
            
            return memory_usage_mb
        except ImportError:
            logger.warning("psutil module not available for memory monitoring")
            logger.debug("Install with: pip install psutil")
            return None
        except Exception as e:
            logger.warning(f"Could not monitor memory: {e}")
            return None
    
    @staticmethod
    def get_memory_usage() -> Optional[Dict[str, Any]]:
        """
        Get current memory usage statistics without logging
        
        Returns:
            Dictionary with memory usage information or None if monitoring failed
        """
        try:
            import psutil
            import os
            
            # Get the current process
            process = psutil.Process(os.getpid())
            
            # Get memory info
            memory_info = process.memory_info()
            
            # Calculate memory usage in MB
            memory_usage_mb = memory_info.rss / (1024 * 1024)
            
            # Get system memory info for context
            system_memory = psutil.virtual_memory()
            system_total_gb = system_memory.total / (1024**3)
            system_used_percent = system_memory.percent
            
            return {
                'process_memory_mb': memory_usage_mb,
                'system_total_gb': system_total_gb,
                'system_used_percent': system_used_percent
            }
        except ImportError:
            return None
        except Exception:
            return None
    
    @staticmethod
    def is_memory_critical(threshold_percent: float = 90.0) -> Tuple[bool, Optional[float]]:
        """
        Check if system memory usage is at a critical level
        
        Args:
            threshold_percent: Percentage threshold for critical level
            
        Returns:
            Tuple of (is_critical, current_percent)
        """
        try:
            import psutil
            
            # Get system memory info
            system_memory = psutil.virtual_memory()
            used_percent = system_memory.percent
            
            return used_percent > threshold_percent, used_percent
        except ImportError:
            return False, None
        except Exception:
            return False, None
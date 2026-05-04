# forge_engine/masker/masker.py
"""
Main masker class for data masking operations in the forge_engine package.
Orchestrates the masking process according to configuration.
"""

import os
import yaml
import pandas as pd
import time
from pathlib import Path
from typing import Dict, Any, Optional, List

class Masker:
    """
    Main entry point for data masking operations.
    Coordinates the masking process according to configuration.
    """

    def __init__(self, config_path: str):
        """
        Initialize the masker with a configuration file
        
        Args:
            config_path: Path to the YAML configuration file
        """
        # Load configuration
        with open(config_path, 'r') as f:
            self.config = yaml.safe_load(f)
        
        # Add default global settings if not present
        if 'global_settings' not in self.config:
            self.config['global_settings'] = {}
        
        if 'logging' not in self.config:
            self.config['logging'] = {}

        # Initialize logger
        from forge_engine.utils.logging import get_logger
        self.logger = get_logger(self.config['logging'])
        
        # Memory monitoring
        from forge_engine.utils.memory import MemoryMonitor
        MemoryMonitor.monitor_memory_usage(self.logger, "After initialization")

        # Validate configuration
        from forge_engine.core.validator import MaskerConfigValidator
        self.validator = MaskerConfigValidator(self.logger)
        self.validator.validate(self.config)
        
        # Get seed from config
        seed = self.config.get('global_settings', {}).get('seed', 42)
        
        # Initialize I/O handler
        from forge_engine.connectors.factory import create_io_handler
        self.io_handler = create_io_handler(
            self.config['input'], 
            self.config['output'], 
            self.config,
            self.logger
        )
        
        # Initialize strategy manager
        from forge_engine.transforms.registry import StrategyManager
        self.strategy_manager = StrategyManager(seed, self.logger)
        
        # Initialize referential integrity manager
        from forge_engine.core.integrity import ReferentialIntegrityManager
        self.ref_integrity = ReferentialIntegrityManager(self.config, self.logger)
        
        # Initialize processor
        from forge_engine.masker.processor import MaskingProcessor
        self.processor = MaskingProcessor(
            self.config, 
            self.strategy_manager, 
            self.ref_integrity, 
            self.logger
        )
        
        # Initialize large file processor
        from forge_engine.utils.processor import LargeFileProcessor
        self.large_file_processor = LargeFileProcessor(self.config, self.logger)
        
        # Initialize mappings directory
        mappings_dir = self.config.get('mappings', {}).get('store_directory', 'mappings/')
        Path(mappings_dir).mkdir(parents=True, exist_ok=True)
        
        self.logger.info(f"Masker initialized with configuration: {config_path}")

    def mask(self):
        """
        Orchestrates the masking process: loads input data, applies rules, and saves masked output.
        Automatically switches to chunked processing for large files.
        """
        try:
            # Get input path and table name
            input_path = self.config['input'].get('path', '')
            table_name = os.path.splitext(os.path.basename(input_path))[0]
            
            self.logger.info(f"=== Starting masking process for table: {table_name} ===")
            self.logger.info(f"Input path: {input_path}")
            self.logger.info(f"Output path: {self.config['output'].get('path', '')}")

            from forge_engine.utils.memory import MemoryMonitor
            MemoryMonitor.monitor_memory_usage(self.logger, "Before masking process")
            
            # Check if input file exists
            if not os.path.exists(input_path):
                error_msg = f"Input file not found: {input_path}"
                self.logger.error(error_msg)
                raise FileNotFoundError(error_msg)
            
            # Check file size for chunked processing
            file_size_gb = os.path.getsize(input_path) / (1024 ** 3)
            large_file_threshold_gb = self.config.get('global_settings', {}).get('large_file_threshold_gb', 1.0)
            
            self.logger.info(f"File size: {file_size_gb:.2f} GB")
            self.logger.info(f"Large file threshold: {large_file_threshold_gb} GB")
            
            if file_size_gb > large_file_threshold_gb:
                self._process_large_file(input_path, table_name, file_size_gb)
            else:
                self._process_standard_file(input_path, table_name, file_size_gb)
            
            # Save global mappings
            self.logger.info("Saving global mappings")
            self.ref_integrity.save_global_mappings()
            
            MemoryMonitor.monitor_memory_usage(self.logger, "After masking completion")
            
            self.logger.info("=== Masking process completed successfully ===")
        except Exception as e:
            self.logger.error(f"Error during masking process: {str(e)}")
            self.logger.error(f"Error type: {type(e).__name__}")
            
            # Log detailed traceback
            import traceback
            tb_str = traceback.format_exc()
            self.logger.error(f"Traceback:\n{tb_str}")
            
            self.logger.error("=== Masking process failed ===")
            raise
        finally:
            # Close masker if needed
            if hasattr(self.io_handler, 'close'):
                self.logger.debug("Closing I/O handler resources")
                self.io_handler.close()
    
    def _process_large_file(self, input_path: str, table_name: str, file_size_gb: float):
        """
        Process large files using chunked processing
        
        Args:
            input_path: Path to the input file
            table_name: Name of the table being processed
            file_size_gb: Size of the file in GB
        """
        self.logger.info(f"Large file detected ({file_size_gb:.2f} GB). Using chunked processing.")
        
        # Get CSV file schema without loading entire file
        self.logger.debug("Loading sample rows to determine schema")
        df_sample = self.io_handler.load_sample(5)
        self.logger.debug(f"Sample columns: {', '.join(df_sample.columns.tolist())}")
        
        # Define processing function
        def process_chunk(chunk):
            return self.processor.apply_masking_rules_to_chunk(chunk, table_name)
        
        # Process large dataset in chunks
        self.large_file_processor.process_large_dataset(
            input_path=input_path,
            df_schema=df_sample,
            processor_func=process_chunk,
            output_path=self.config['output'].get('path', ''),
            description="Masking data"
        )
    
    def _process_standard_file(self, input_path: str, table_name: str, file_size_gb: float):
        """
        Process standard-sized files using in-memory processing
        
        Args:
            input_path: Path to the input file
            table_name: Name of the table being processed
            file_size_gb: Size of the file in GB
        """
        self.logger.info(f"Standard processing for file size: {file_size_gb:.2f} GB")
        start_time = time.time()  # Track execution time
        
        from forge_engine.utils.memory import MemoryMonitor
        MemoryMonitor.monitor_memory_usage(self.logger, "Before loading data")
        
        self.logger.info(f"Loading data from {input_path}")
        df = self.io_handler.load_data()

        MemoryMonitor.monitor_memory_usage(self.logger, "After loading data")
        
        load_time = time.time() - start_time
        self.logger.info(f"Data loaded in {load_time:.2f} seconds. Rows: {len(df)}, Columns: {len(df.columns)}")
        
        # Apply masking rules
        df = self.processor.apply_masking_rules(df, table_name)
        
        MemoryMonitor.monitor_memory_usage(self.logger, "After applying masking rules")

        # Save output
        output_path = self.config['output'].get('path', '')
        self.logger.info(f"Saving masked data to {output_path}")
        
        MemoryMonitor.monitor_memory_usage(self.logger, "Before saving data")

        save_start_time = time.time()
        self.io_handler.save_data(df)
        save_time = time.time() - save_start_time
        
        MemoryMonitor.monitor_memory_usage(self.logger, "After saving data")

        self.logger.info(f"Data saved in {save_time:.2f} seconds")
        
        # Total execution time
        total_time = time.time() - start_time
        self.logger.info(f"Total masking process completed in {total_time:.2f} seconds")
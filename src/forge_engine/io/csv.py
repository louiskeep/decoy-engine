# forge_engine/io/csv.py
"""
CSV I/O functionality for the forge_engine package.
"""

import os
import pandas as pd
from typing import Dict, Any, Optional, List
from pathlib import Path

from forge_engine.io.base import IOHandler
from forge_engine.utils.helpers import convert_quoting_mode, create_directory_for_file


class CSVHandler(IOHandler):
    """
    I/O handler for CSV/delimited files.
    Handles loading and saving data from CSV format.
    """
    
    def __init__(self, input_config: Dict[str, Any], output_config: Dict[str, Any], logger=None):
        """
        Initialize with input and output configurations
        
        Args:
            input_config: Dictionary with input configuration
            output_config: Dictionary with output configuration
            logger: Logger instance (optional)
        """
        super().__init__(input_config, output_config, logger)
    
    def load_data(self) -> pd.DataFrame:
        """
        Load data from a CSV/delimited file
        
        Returns:
            pandas.DataFrame: The loaded data with column headers
        """
        input_path = self.input_config['path']
        # Get delimiter from config or default to comma
        delimiter = self.input_config.get('csv_options', {}).get('delimiter', ',')
        # Get other CSV options
        encoding = self.input_config.get('csv_options', {}).get('encoding', 'utf-8')
        
        self.logger.info(f"Loading CSV data from: {input_path}")
        self.logger.debug(f"CSV options: delimiter='{delimiter}', encoding='{encoding}'")
        
        try:
            # Load with header (now required)
            self.logger.debug("Reading CSV file with pandas")
            df = pd.read_csv(
                input_path, 
                delimiter=delimiter,
                encoding=encoding,
                header=0
            )
            
            self.logger.info(f"Successfully loaded {len(df)} rows with {len(df.columns)} columns")
            self.logger.debug(f"Columns: {', '.join(df.columns.tolist())}")
            return df
            
        except Exception as e:
            self.logger.error(f"Failed to load CSV file: {input_path}")
            if "No columns to parse from file" in str(e):
                self.logger.error(f"Ensure the file has headers: {e}")
                raise ValueError(f"Failed to load CSV file. Ensure the file has headers: {e}")
            else:
                self.logger.error(f"Error details: {str(e)}")
                raise ValueError(f"Failed to load CSV file: {e}")
    
    def save_data(self, df: pd.DataFrame) -> None:
        """
        Save the data to a CSV/delimited file (or fixed-width if output type specifies it)

        Args:
            df: The pandas DataFrame to save
        """
        if self.output_config.get('type') == 'fixed_width':
            from forge_engine.io.fixed_width import FixedWidthHandler
            fw = FixedWidthHandler(None, self.output_config, self.logger)
            fw._save_as_fixed_width(df)
            return

        # Ensure output directory exists
        output_path = self.output_config['path']
        self.logger.info(f"Saving data to: {output_path}")
        
        # Create directory if it doesn't exist
        create_directory_for_file(output_path)
        self.logger.debug(f"Ensured output directory exists: {os.path.dirname(output_path)}")
        
        # Get delimiter from config or default to comma
        delimiter = self.output_config.get('csv_options', {}).get('delimiter', ',')
        # Get other CSV options
        encoding = self.output_config.get('csv_options', {}).get('encoding', 'utf-8')
        # Get quoting mode
        quoting_mode = self.output_config.get('csv_options', {}).get('quoting', 'minimal')
        quoting = convert_quoting_mode(quoting_mode)
        
        self.logger.debug(f"CSV output options: delimiter='{delimiter}', encoding='{encoding}', quoting='{quoting_mode}'")
        
        try:
            df.to_csv(
                output_path, 
                index=False,
                sep=delimiter,
                encoding=encoding,
                quoting=quoting
            )
            self.logger.info(f"Successfully saved {len(df)} rows to {output_path}")
        except Exception as e:
            self.logger.error(f"Failed to save CSV file: {output_path}")
            self.logger.error(f"Error details: {str(e)}")
            raise
    
    def load_sample(self, sample_rows: int = 5) -> pd.DataFrame:
        """
        Load a sample of rows from the CSV file to get schema
        
        Args:
            sample_rows: Number of rows to load
            
        Returns:
            pandas.DataFrame with the sample rows
        """
        input_path = self.input_config['path']
        delimiter = self.input_config.get('csv_options', {}).get('delimiter', ',')
        encoding = self.input_config.get('csv_options', {}).get('encoding', 'utf-8')
        
        self.logger.debug(f"Loading {sample_rows} sample rows from: {input_path}")
        
        try:
            # Load with header
            df_sample = pd.read_csv(
                input_path, 
                delimiter=delimiter,
                encoding=encoding,
                header=0,
                nrows=sample_rows
            )
            
            self.logger.debug(f"Loaded sample with {len(df_sample)} rows and {len(df_sample.columns)} columns")
            return df_sample
            
        except Exception as e:
            self.logger.error(f"Failed to load sample from CSV file: {input_path}")
            self.logger.error(f"Error details: {str(e)}")
            raise ValueError(f"Failed to load sample from CSV file: {e}")
    
    def append_data(self, df: pd.DataFrame) -> None:
        """
        Append data to an existing CSV file
        
        Args:
            df: The pandas DataFrame to append
        """
        output_path = self.output_config['path']
        self.logger.debug(f"Appending {len(df)} rows to: {output_path}")
        
        # Get CSV options
        delimiter = self.output_config.get('csv_options', {}).get('delimiter', ',')
        encoding = self.output_config.get('csv_options', {}).get('encoding', 'utf-8')
        quoting_mode = self.output_config.get('csv_options', {}).get('quoting', 'minimal')
        quoting = convert_quoting_mode(quoting_mode)
        
        try:
            df.to_csv(
                output_path,
                mode='a',
                header=False,
                index=False,
                sep=delimiter,
                encoding=encoding,
                quoting=quoting
            )
            self.logger.debug(f"Successfully appended {len(df)} rows to {output_path}")
        except Exception as e:
            self.logger.error(f"Failed to append to CSV file: {output_path}")
            self.logger.error(f"Error details: {str(e)}")
            raise
    
    def get_chunk_iterator(self, chunk_size: int = 100000):
        """
        Get an iterator for processing the CSV file in chunks
        
        Args:
            chunk_size: Number of rows in each chunk
            
        Returns:
            Iterator yielding DataFrame chunks
        """
        input_path = self.input_config['path']
        delimiter = self.input_config.get('csv_options', {}).get('delimiter', ',')
        encoding = self.input_config.get('csv_options', {}).get('encoding', 'utf-8')
        
        self.logger.info(f"Processing {input_path} in chunks of {chunk_size} rows")
        
        try:
            # Return a chunk iterator
            return pd.read_csv(
                input_path,
                delimiter=delimiter,
                encoding=encoding,
                header=0,
                chunksize=chunk_size
            )
        except Exception as e:
            self.logger.error(f"Failed to create chunk iterator for CSV file: {input_path}")
            self.logger.error(f"Error details: {str(e)}")
            raise ValueError(f"Failed to process CSV file in chunks: {e}")
    
    def count_rows(self) -> int:
        """
        Count the number of rows in the CSV file
        
        Returns:
            Number of rows in the file (excluding header)
        """
        input_path = self.input_config['path']
        encoding = self.input_config.get('csv_options', {}).get('encoding', 'utf-8')
        
        self.logger.debug(f"Counting rows in: {input_path}")
        
        try:
            # Count rows by iterating through the file
            with open(input_path, 'r', encoding=encoding) as f:
                # Count total lines and subtract header
                row_count = sum(1 for _ in f) - 1
                
            self.logger.debug(f"File contains {row_count} rows (excluding header)")
            return row_count
        except Exception as e:
            self.logger.warning(f"Failed to count rows in CSV file: {input_path}")
            self.logger.warning(f"Error details: {str(e)}")
            return -1  # Return -1 to indicate error
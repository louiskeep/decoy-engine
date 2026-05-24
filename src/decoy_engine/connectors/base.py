"""
Base I/O functionality for the decoy_engine package.
"""

from abc import ABC, abstractmethod
from typing import Any

import pandas as pd


class IOHandler(ABC):
    """
    Abstract base class for I/O operations.
    Handles loading and saving data from different file formats.
    """

    def __init__(self, input_config: dict[str, Any], output_config: dict[str, Any], logger=None):
        """
        Initialize with input and output configurations

        Args:
            input_config: Dictionary with input configuration
            output_config: Dictionary with output configuration
            logger: Logger instance (optional)
        """
        self.input_config = input_config
        self.output_config = output_config

        # Use provided logger or create a default one
        if logger:
            self.logger = logger
        else:
            from decoy_engine.internal.logging import get_logger

            self.logger = get_logger()

    @abstractmethod
    def load_data(self) -> pd.DataFrame:
        """
        Load data from the source

        Returns:
            pandas.DataFrame: The loaded data
        """
        pass

    @abstractmethod
    def save_data(self, df: pd.DataFrame) -> None:
        """
        Save the data to the destination

        Args:
            df: The pandas DataFrame to save
        """
        pass

    def close(self) -> None:
        """
        Close any resources that need to be cleaned up
        """
        self.logger.debug("Closing I/O resources")

    def validate_config(self) -> None:
        """
        Validate that the configuration has all required fields

        Raises:
            ValueError: If configuration is invalid
        """
        self.logger.debug("Validating I/O configuration")
        if "path" not in self.input_config:
            raise ValueError("Input configuration must specify 'path'")

        if "path" not in self.output_config:
            raise ValueError("Output configuration must specify 'path'")

    def chunk_dataframe(self, df: pd.DataFrame, chunk_size: int = 100000) -> list[pd.DataFrame]:
        """
        Split dataframe into chunks for processing large datasets

        Args:
            df: The pandas DataFrame to chunk
            chunk_size: Number of rows per chunk

        Returns:
            List of DataFrame chunks
        """
        num_chunks = (len(df) + chunk_size - 1) // chunk_size
        self.logger.debug(f"Splitting dataframe into {num_chunks} chunks of size {chunk_size}")
        return [df.iloc[i : i + chunk_size] for i in range(0, len(df), chunk_size)]

    @property
    def input_path(self) -> str:
        """
        Get the input file path

        Returns:
            String path to the input file
        """
        return self.input_config.get("path", "")

    @property
    def output_path(self) -> str:
        """
        Get the output file path

        Returns:
            String path to the output file
        """
        return self.output_config.get("path", "")

    def get_file_size_info(self) -> str:
        """
        Get information about the input file size

        Returns:
            Human-readable string with file size information
        """
        from decoy_engine.internal.helpers import convert_file_size, get_file_size

        size_bytes = get_file_size(self.input_path)
        if size_bytes is None:
            return "Input file not found"

        return f"Input file size: {convert_file_size(size_bytes)}"

    def set_column_configurations(self, column_configs: list[dict[str, Any]]) -> None:
        """
        Set column configurations for formatting purposes
        Default implementation does nothing

        Args:
            column_configs: List of column configuration dictionaries
        """
        pass

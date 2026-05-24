from abc import ABC, abstractmethod
from typing import Any

import pandas as pd


class BaseMasker(ABC):
    """
    Abstract base class for data masking operations
    """

    def __init__(self, input_config: dict[str, Any], output_config: dict[str, Any], logger=None):
        """
        Initialize with input and output configurations

        Args:
            input_config: Dictionary with input configuration
            output_config: Dictionary with output configuration
            logger: MaskerLogger instance (optional)
        """
        self.input_config = input_config
        self.output_config = output_config

        # Use provided logger or create a default one
        if logger:
            self.logger = logger
        else:
            from .logger import MaskerLogger

            self.logger = MaskerLogger.get_logger()

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
        Save the masked data to the destination

        Args:
            df: The pandas DataFrame to save
        """
        pass

    def close(self) -> None:
        """
        Close any resources that need to be cleaned up
        """
        self.logger.debug("Closing masker resources")
        pass

    def validate_config(self) -> None:
        """
        Validate that the configuration has all required fields

        Raises:
            ValueError: If configuration is invalid
        """
        self.logger.debug("Validating masker configuration")
        pass

    def chunk_dataframe(self, df: pd.DataFrame, chunk_size: int = 100000) -> list:
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


class ConfigValidator(ABC):
    """
    Abstract base class for configuration validators.
    """

    def __init__(self, logger=None):
        """
        Initialize with optional logger

        Args:
            logger: Logger instance (optional)
        """
        # Use provided logger or create a default one
        if logger:
            self.logger = logger
        else:
            from decoy_engine.internal.logging import get_logger

            self.logger = get_logger()

    @abstractmethod
    def validate(self, config: dict[str, Any]) -> None:
        """
        Validate the configuration

        Args:
            config: Configuration dictionary to validate

        Raises:
            ValueError: If validation fails
        """
        pass


class MaskingStrategy(ABC):
    """
    Abstract base class for masking strategies.
    """

    def __init__(self, seed: int = 42, logger=None, derive_key=None):
        """
        Initialize with a seed for deterministic behavior

        Args:
            seed: Random seed for deterministic masking (legacy fallback path)
            logger: Logger instance (optional)
            derive_key: Optional callable ``(info: str) -> bytes`` returning
                32 bytes of HKDF-derived key material. When supplied, keyed
                strategies (hash, faker, date_shift) prefer this over the
                ``seed``-coupled path so output is tenant-scoped and bitwise
                stable across runs and instances.
        """
        self.seed = seed
        self.derive_key = derive_key
        self.strategy_name = self.__class__.__name__.lower().replace("strategy", "")

        # Use provided logger or create a default one
        if logger:
            self.logger = logger
        else:
            from decoy_engine.internal.logging import get_logger

            self.logger = get_logger()

    @abstractmethod
    def apply(self, column: pd.Series, rule: dict[str, Any]) -> pd.Series:
        """
        Apply the masking strategy to a column

        Args:
            column: Pandas Series to mask
            rule: Dictionary containing the masking rule configuration

        Returns:
            Pandas Series with masked values
        """
        pass

    def validate_rule(self, rule: dict[str, Any]) -> None:
        """
        Validate that the rule contains all required fields for this strategy

        Args:
            rule: Dictionary containing the masking rule configuration

        Raises:
            ValueError: If rule validation fails
        """
        pass

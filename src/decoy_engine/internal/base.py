from abc import ABC, abstractmethod
from typing import Any

import pandas as pd


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

    def apply_with_context(
        self,
        column: pd.Series,
        rule: dict[str, Any],
        ctx: Any = None,
    ) -> pd.Series:
        """V2 Phase 3 D5c: apply with runtime ApplyContext.

        Default implementation ignores `ctx` and delegates to
        `apply(column, rule)`. Strategies that don't need joint
        columns or other dispatcher-resolved runtime state get this
        no-op routing for free - no signature changes required.

        Strategies that DO need ctx (D5c hash joint preservation,
        future date_shift subject-key work, etc.) should override
        THIS method instead of `apply`. The base `apply` remains
        the single-column contract; `apply_with_context` is the
        joint-aware entry point the dispatcher calls.

        Args:
            column: Pandas Series to mask
            rule: Dictionary containing the masking rule configuration
            ctx: An `ApplyContext` (or None to fall through to
                single-column `apply`). Type-annotated as `Any` here
                to avoid a base.py -> apply_context cyclic import;
                callers and overrides see the real type.

        Returns:
            Pandas Series with masked values
        """
        return self.apply(column, rule)

    def validate_rule(self, rule: dict[str, Any]) -> None:
        """
        Validate that the rule contains all required fields for this strategy

        Args:
            rule: Dictionary containing the masking rule configuration

        Raises:
            ValueError: If rule validation fails
        """
        pass

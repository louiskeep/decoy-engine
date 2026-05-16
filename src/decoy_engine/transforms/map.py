# decoy_engine/strategies/map.py
"""
Map masking strategy for the decoy_engine package.
Uses deterministic value-level transforms without local mapping storage.
"""

import pandas as pd
from typing import Dict, Any

from decoy_engine.transforms.base import BaseMaskingStrategy
from decoy_engine.internal.helpers import (
    deterministic_hash,
    get_faker_providers,
    make_faker,
)


class MapStrategy(BaseMaskingStrategy):
    """
    Masking strategy for explicit map-style replacements.

    V1 policy forbids local mapping stores. Every output is derived from the
    input value plus seed/rule config, so repeated values stay consistent
    without writing mappings to disk.
    """

    def __init__(self, seed: int = 42, logger=None):
        """
        Initialize the map strategy with seed for deterministic behavior.

        Args:
            seed: Random seed for deterministic masking
            logger: Logger instance (optional)
        """
        super().__init__(seed, logger)

    def apply(self, column: pd.Series, rule: Dict[str, Any]) -> pd.Series:
        """
        Apply deterministic map-style replacement without mapping storage.

        Args:
            column: Pandas Series to mask
            rule: Dictionary containing the masking rule configuration

        Returns:
            Pandas Series with mapped values
        """
        column_name = rule['column']
        map_type = rule.get('map_type', 'faker')
        seed = rule.get('seed', self.seed)
        faker_type = rule.get('faker_type', 'word')

        self.logger.debug(
            f"Map configuration: type='{map_type}', seed={seed}, "
            f"faker_type='{faker_type}'"
        )

        faker_kwargs = rule.get('faker_kwargs') or {}
        if not isinstance(faker_kwargs, dict):
            self.logger.warning(
                f"map: faker_kwargs must be a mapping, got "
                f"{type(faker_kwargs).__name__}; ignoring"
            )
            faker_kwargs = {}

        result = column.apply(
            lambda value: self._map_value(
                value, column_name, map_type, seed, faker_type, rule, faker_kwargs
            )
        )

        self._log_stats(column, result, rule)
        return result

    def _map_value(
        self,
        value,
        column_name: str,
        map_type: str,
        seed: int,
        faker_type: str,
        rule: Dict[str, Any],
        faker_kwargs: Dict[str, Any],
    ):
        if value is None or pd.isna(value):
            return value

        str_value = str(value)

        if map_type == 'faker':
            return self._faker_value(
                str_value, column_name, seed, faker_type, rule, faker_kwargs
            )
        if map_type == 'hash':
            return deterministic_hash(str_value, seed)
        if map_type == 'fixed':
            prefix = rule.get('fixed_prefix', 'MASKED')
            suffix = deterministic_hash(f"{column_name}:{str_value}", seed)[:12]
            return f"{prefix}_{suffix}"
        if map_type == 'manual':
            explicit = rule.get('mapping', {})
            return explicit.get(str_value, str_value)

        self.logger.warning(f"Unknown map_type '{map_type}', using 'hash' instead")
        return deterministic_hash(str_value, seed)

    def _faker_value(
        self,
        str_value: str,
        column_name: str,
        seed: int,
        faker_type: str,
        rule: Dict[str, Any],
        faker_kwargs: Dict[str, Any],
    ):
        fake = make_faker(rule.get('locale'))
        fake.seed_instance(self._seed_for_value(column_name, str_value, seed))
        providers = get_faker_providers(fake)

        if faker_type == 'email' and rule.get('preserve_domain', False) and '@' in str_value:
            _, domain = str_value.split('@', 1)
            self.logger.debug(f"Preserving domain for email: {domain}")
            return f"{fake.user_name()}@{domain}"

        if faker_type in providers:
            return providers[faker_type](**faker_kwargs)

        self.logger.warning(f"Unknown faker_type '{faker_type}', using 'word' instead")
        return providers['word']()

    def _seed_for_value(self, column_name: str, value: str, seed: int) -> int:
        return int(deterministic_hash(f"{column_name}:{value}", seed)[:8], 16)

    def validate_rule(self, rule: Dict[str, Any]) -> None:
        """
        Validate that the rule contains all required fields for the map strategy.

        Args:
            rule: Dictionary containing the masking rule configuration

        Raises:
            ValueError: If rule validation fails
        """
        super().validate_rule(rule)

        # Set default map_type if not specified
        if 'map_type' not in rule:
            rule['map_type'] = 'faker'
            self.logger.debug(f"Using default map_type: 'faker' for column '{rule['column']}'")

        # Check for faker_type if map_type is 'faker'
        if rule['map_type'] == 'faker' and 'faker_type' not in rule:
            rule['faker_type'] = 'word'
            self.logger.debug(f"Using default faker_type: 'word' for column '{rule['column']}'")

        # Check for fixed_prefix if map_type is 'fixed'
        if rule['map_type'] == 'fixed' and 'fixed_prefix' not in rule:
            rule['fixed_prefix'] = 'MASKED'
            self.logger.debug(f"Using default fixed_prefix: 'MASKED' for column '{rule['column']}'")

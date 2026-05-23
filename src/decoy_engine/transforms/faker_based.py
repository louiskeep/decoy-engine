"""
Faker masking strategy for the decoy_engine package.

Replaces values with realistic fake data using the Faker library. Both
the keyed and the seeded-fallback paths derive each output from the
input value, so duplicate source values always map to duplicate fake
values. No state is stored locally; reruns reproduce the same outputs
given the same key (or seed).

Pattern: Faker seeded deterministic generation (joke2k/faker, MIT).
  Faker: https://faker.readthedocs.io/
  Determinism via Faker(...).seed_instance(seed) per value.
"""

from typing import Any

import pandas as pd

from decoy_engine.internal.helpers import (
    deterministic_hash,
    get_faker_providers,
    hmac_seed,
    make_faker,
)
from decoy_engine.transforms.base import BaseMaskingStrategy


class FakerStrategy(BaseMaskingStrategy):
    """
    Masking strategy that replaces values with realistic fake data.

    Both keyed and seeded fallback paths derive each output from the input
    value. No map state is stored locally, and duplicate source values produce
    duplicate fake values.
    """

    def __init__(self, seed: int = 42, logger=None, derive_key=None):
        super().__init__(seed, logger, derive_key=derive_key)

    def apply(self, column: pd.Series, rule: dict[str, Any]) -> pd.Series:
        faker_type = rule.get('faker_type', 'word')
        column_name = rule.get('column', 'unnamed')
        column_key = self._column_key(column_name)
        preserve_domain = rule.get('preserve_domain', False) and faker_type == 'email'
        locale = rule.get('locale')
        faker_kwargs = rule.get('faker_kwargs') or {}
        if not isinstance(faker_kwargs, dict):
            self.logger.warning(
                f"faker_kwargs for column {column_name!r} must be a mapping, "
                f"got {type(faker_kwargs).__name__}; ignoring"
            )
            faker_kwargs = {}

        if column_key is not None:
            self.logger.debug(
                f"Applying keyed faker (type={faker_type!r}, locale={locale!r}) "
                f"to column '{column_name}'"
            )
            seed_for = lambda value: hmac_seed(column_key, value)
        else:
            rule_seed = rule.get('seed', self.seed)
            self.logger.debug(
                f"Applying seeded faker (type={faker_type!r}, seed={rule_seed}, "
                f"locale={locale!r}) to column '{column_name}'"
            )
            seed_for = lambda value: int(
                deterministic_hash(f"{column_name}:{value}", rule_seed)[:8],
                16,
            )

        result = self._apply_value_seeded(
            column, seed_for, faker_type, preserve_domain, locale, rule,
            faker_kwargs,
        )
        self._log_stats(column, result, rule)
        return result

    def _apply_value_seeded(
        self,
        column: pd.Series,
        seed_for,
        faker_type: str,
        preserve_domain: bool,
        locale,
        rule: dict[str, Any],
        faker_kwargs: dict[str, Any],
    ) -> pd.Series:
        # Cache is local to this call and only avoids repeated Faker setup for
        # duplicate values. It is not persisted and does not define behavior.
        cache: dict[Any, Any] = {}

        def fake_for(value):
            if value is None or pd.isna(value):
                return value
            cache_key = str(value)
            if cache_key in cache:
                return cache[cache_key]

            fake = make_faker(locale)
            fake.seed_instance(seed_for(value))

            if preserve_domain and '@' in str(value):
                _, domain = str(value).split('@', 1)
                out = f"{fake.user_name()}@{domain}"
            else:
                providers = get_faker_providers(fake)
                if faker_type in providers:
                    out = providers[faker_type](**faker_kwargs)
                else:
                    self.logger.warning(
                        f"Unknown faker_type {faker_type!r}, using 'word' instead"
                    )
                    out = providers['word']()

            cache[cache_key] = out
            return out

        return column.apply(fake_for)

    def _column_key(self, column_name: str) -> bytes | None:
        """Derive the mask subkey via the caller-supplied resolver."""
        if self.derive_key is None:
            return None
        try:
            return self.derive_key("mask")
        except Exception as exc:
            self.logger.warning(
                f"derive_key failed for 'mask' ({exc}); falling back to seeded faker"
            )
            return None

    def validate_rule(self, rule: dict[str, Any]) -> None:
        """
        Validate that the rule contains all required fields for the faker strategy.

        Args:
            rule: Dictionary containing the masking rule configuration

        Raises:
            ValueError: If rule validation fails
        """
        super().validate_rule(rule)

        # Faker-specific validation
        if 'faker_type' not in rule:
            # Default to 'word' if not specified
            rule['faker_type'] = 'word'
            self.logger.debug(f"Using default faker_type: 'word' for column '{rule['column']}'")

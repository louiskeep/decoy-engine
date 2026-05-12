# decoy_engine/strategies/faker.py
"""
Faker masking strategy for the decoy_engine package.
Replaces values with realistic fake data using the Faker library.
"""

import pandas as pd
import random
from typing import Dict, Any, Optional

from decoy_engine.transforms.base import BaseMaskingStrategy
from decoy_engine.internal.helpers import get_faker_providers, hmac_seed, make_faker


class FakerStrategy(BaseMaskingStrategy):
    """
    Masking strategy that replaces values with realistic fake data.
    Uses the Faker library to generate fake values that look realistic.

    Two paths:
      * **Keyed (preferred — "Path B").** When ``derive_key`` is configured,
        each input value gets its own per-value seed via
        ``hmac_seed(column_key, value)``. A fresh ``Faker`` instance is
        seeded with that integer and called once. Same input + same key →
        same fake output, with NO map state stored anywhere. Output is
        bitwise stable across runs and across machines.
      * **Legacy (fallback).** Without a master key, falls back to the
        prior in-memory map built by iterating ``column.unique()``. This
        path is row-order dependent — it stays for backwards compat but
        is not bitwise stable across runs.
    """

    def __init__(self, seed: int = 42, logger=None, derive_key=None):
        super().__init__(seed, logger, derive_key=derive_key)
        # Legacy: set Python's random seed for the fallback path.
        random.seed(self.seed)

    def apply(self, column: pd.Series, rule: Dict[str, Any]) -> pd.Series:
        faker_type = rule.get('faker_type', 'word')
        column_name = rule.get('column', 'unnamed')
        column_key = self._column_key(column_name)
        preserve_domain = rule.get('preserve_domain', False) and faker_type == 'email'
        locale = rule.get('locale')
        # Per-provider keyword args from YAML's ``faker_kwargs:`` map.
        # These are passed straight through to the underlying Faker method
        # (e.g. ``representation='alpha-3'`` for ``country_code``, or
        # ``minimum_age=18, maximum_age=25`` for ``date_of_birth``). The
        # provider lambda silently drops kwargs the method doesn't accept,
        # so a stale YAML doesn't crash the run.
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
            return self._apply_keyed(
                column, column_key, faker_type, preserve_domain, locale,
                rule, faker_kwargs,
            )

        rule_seed = rule.get('seed', self.seed)
        self.logger.debug(
            f"Applying legacy faker (type={faker_type!r}, seed={rule_seed}, "
            f"locale={locale!r}) — row-order dependent"
        )
        return self._apply_legacy(
            column, rule_seed, faker_type, preserve_domain, locale,
            rule, faker_kwargs,
        )

    # ── Path B: keyed, stateless, bitwise stable ───────────────────────────

    def _apply_keyed(
        self,
        column: pd.Series,
        column_key: bytes,
        faker_type: str,
        preserve_domain: bool,
        locale,
        rule: Dict[str, Any],
        faker_kwargs: Dict[str, Any],
    ) -> pd.Series:
        # Cache per-input outputs so duplicate values in a column don't pay
        # the Faker construction cost twice. Cache is process-local (reset
        # per call); does not affect determinism.
        cache: Dict[Any, Any] = {}

        def fake_for(val):
            if val is None or pd.isna(val):
                return val
            if val in cache:
                return cache[val]
            seed_int = hmac_seed(column_key, val)
            f = make_faker(locale)
            f.seed_instance(seed_int)
            if preserve_domain and '@' in str(val):
                _, domain = str(val).split('@', 1)
                out = f"{f.user_name()}@{domain}"
            else:
                providers = get_faker_providers(f)
                if faker_type in providers:
                    out = providers[faker_type](**faker_kwargs)
                else:
                    self.logger.warning(
                        f"Unknown faker_type {faker_type!r}, using 'word' instead"
                    )
                    out = providers['word']()
            cache[val] = out
            return out

        result = column.apply(fake_for)
        self._log_stats(column, result, rule)
        return result

    # ── Legacy: in-memory map built from row-encounter order ───────────────

    def _apply_legacy(
        self,
        column: pd.Series,
        rule_seed: int,
        faker_type: str,
        preserve_domain: bool,
        locale,
        rule: Dict[str, Any],
        faker_kwargs: Dict[str, Any],
    ) -> pd.Series:
        deterministic_faker = make_faker(locale)
        deterministic_faker.seed_instance(rule_seed)
        faker_providers = get_faker_providers(deterministic_faker)

        unique_values = column.unique()
        faker_map: Dict[Any, Any] = {}
        for value in unique_values:
            if value is None or pd.isna(value):
                faker_map[value] = value
                continue
            if preserve_domain and '@' in str(value):
                _, domain = str(value).split('@', 1)
                faker_map[value] = f"{deterministic_faker.user_name()}@{domain}"
                continue
            if faker_type in faker_providers:
                faker_map[value] = faker_providers[faker_type](**faker_kwargs)
            else:
                self.logger.warning(
                    f"Unknown faker_type {faker_type!r}, using 'word' instead"
                )
                faker_map[value] = faker_providers['word']()

        result = column.map(faker_map)
        self._log_stats(column, result, rule)
        return result

    # ── helpers ────────────────────────────────────────────────────────────

    def _column_key(self, column_name: str) -> Optional[bytes]:
        """Derive the mask subkey via the caller-supplied resolver. Same as
        HashStrategy._column_key — instance-master-only, no per-column
        tagging. ``column_name`` is kept for log context only."""
        if self.derive_key is None:
            return None
        try:
            return self.derive_key("mask")
        except Exception as exc:
            self.logger.warning(
                f"derive_key failed for 'mask' ({exc}); falling back to legacy faker"
            )
            return None
    
    def validate_rule(self, rule: Dict[str, Any]) -> None:
        """
        Validate that the rule contains all required fields for the faker strategy
        
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
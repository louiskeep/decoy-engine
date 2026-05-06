# decoy_engine/strategies/hash.py
"""
Hash masking strategy for the decoy_engine package.
Replaces values with deterministic hash values.
"""

import pandas as pd
from typing import Dict, Any, Optional

from decoy_engine.transforms.base import BaseMaskingStrategy
from decoy_engine.internal.helpers import deterministic_hash, hmac_hex


class HashStrategy(BaseMaskingStrategy):
    """
    Masking strategy that replaces values with deterministic hash strings.

    Two paths:
      * **Keyed (preferred).** When the engine is configured with a master
        key (``ctx.derive_key``), this becomes HMAC-SHA256(column_key, value).
        Output is bitwise stable across runs and instances given the same
        master key + column name.
      * **Legacy (fallback).** Without a master key, falls back to
        SHA256(value + seed). Same input + same seed → same output, but
        derivable from the value alone, so don't use across tenants.
    """

    def apply(self, column: pd.Series, rule: Dict[str, Any]) -> pd.Series:
        """
        Replace values with deterministic hash strings that can't be reversed
        """
        column_name = rule.get('column', 'unnamed')
        column_key = self._column_key(column_name)
        seed = rule.get('seed', self.seed)

        if column_key is not None:
            self.logger.debug(f"Applying keyed hash to column '{column_name}'")

            def hash_value(val):
                if val is None or pd.isna(val):
                    return val
                return hmac_hex(column_key, str(val))
        else:
            self.logger.debug(
                f"Applying legacy hash with seed {seed} (no master key configured)"
            )

            def hash_value(val):
                if val is None or pd.isna(val):
                    return val
                return deterministic_hash(str(val), seed)

        result = column.apply(hash_value)
        self._log_stats(column, result, rule)
        return result

    def _column_key(self, column_name: str) -> Optional[bytes]:
        """Derive the mask subkey via the caller-supplied resolver, if one
        was injected. The same input value hashes identically across every
        column, table, and pipeline on the instance — preserves FK joins
        through masking by default, no per-column or per-table tagging
        needed. ``column_name`` is kept in the signature only for log
        context. None means "no master key configured" — fall back to the
        legacy seeded path."""
        if self.derive_key is None:
            return None
        try:
            return self.derive_key("mask")
        except Exception as exc:
            self.logger.warning(
                f"derive_key failed for 'mask' ({exc}); falling back to legacy hash"
            )
            return None
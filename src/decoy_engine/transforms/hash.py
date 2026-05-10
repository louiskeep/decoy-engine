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
        # Optional output-length cap. SHA-256 hex is 64 chars; legacy targets
        # with `CHAR(N)` columns can ask for a shorter slice. Truncation is
        # bitwise stable across runs because it slices a deterministic hash.
        truncate = self._resolve_truncate(rule.get('truncate'), column_name)

        if column_key is not None:
            self.logger.debug(f"Applying keyed hash to column '{column_name}'")
            hash_str = lambda s: hmac_hex(column_key, s)
        else:
            self.logger.debug(
                f"Applying legacy hash with seed {seed} (no master key configured)"
            )
            hash_str = lambda s: deterministic_hash(s, seed)

        # The crypto itself (HMAC-SHA256) has to run once per value — there's
        # no batched-on-the-whole-column equivalent. So this isn't true
        # vectorization; we're just trimming overhead off the per-row loop.
        # Three things move out of the loop into single whole-column ops:
        # the null check (one C-level mask vs N Python `pd.isna` calls), the
        # string cast (one `.astype(str)` vs N `str(val)` calls), and the
        # pandas apply machinery itself (a plain list comp is cheaper than
        # `Series.apply`, which boxes/unboxes every scalar). Worth ~3-6x.
        na_mask = column.isna()
        non_na_str = column[~na_mask].astype(str).tolist()
        if truncate:
            hashed = [hash_str(s)[:truncate] for s in non_na_str]
        else:
            hashed = [hash_str(s) for s in non_na_str]
        result = column.copy().astype(object)
        result.loc[~na_mask] = hashed

        self._log_stats(column, result, rule)
        return result

    def _resolve_truncate(self, raw, column_name: str) -> Optional[int]:
        """Coerce + validate the `truncate` config. None / 0 / missing means
        no truncation. Out-of-range or non-integer values are coerced to None
        with a warning rather than raising — keeps the masking run going on
        a single bad rule. SHA-256 hex is 64 chars, so the cap is 64. Booleans
        are rejected explicitly because in Python `bool` is a subclass of
        `int` and would otherwise pass the type check."""
        if raw is None or raw == 0:
            return None
        if isinstance(raw, bool) or not isinstance(raw, int):
            self.logger.warning(
                f"hash.truncate must be an integer, got {raw!r} for column "
                f"'{column_name}'; ignoring truncate"
            )
            return None
        if raw < 1 or raw > 64:
            self.logger.warning(
                f"hash.truncate={raw} for column '{column_name}' is out of range "
                f"[1, 64]; ignoring truncate"
            )
            return None
        return raw

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
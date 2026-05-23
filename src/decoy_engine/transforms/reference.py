# decoy_engine/transforms/reference.py
"""
Reference masking strategy for the decoy_engine package.

Replaces each input value with one drawn from an external reference dataset.
Lookup is deterministic — same input always lands on the same row of the
reference, so foreign-key joins on the masked column survive masking.

Companion to the synthesis-side `_generate_reference_column` in
`generators/columns.py` (which pulls from a *previously generated* sibling
table). This is the masking-side equivalent: pulls from a static reference
catalog (CSV file today; connector tables and inline lists later).
"""

import hashlib
import hmac
import os
from pathlib import Path
from typing import Any

import pandas as pd

from decoy_engine.transforms.base import BaseMaskingStrategy


class ReferenceStrategy(BaseMaskingStrategy):
    """
    Masking strategy that replaces values with picks from a reference dataset.

    Two determinism paths mirror HashStrategy:
      * **Keyed (preferred).** HMAC-SHA256(column_key, value) → integer
        index → reference[idx]. Output is bitwise stable across runs and
        instances given the same master key + column name.
      * **Legacy (fallback).** SHA256(value + seed) → integer index. Same
        input + same seed → same index, but no per-tenant secret in play.

    Reference dataset is loaded once per `apply()` call and cached on the
    instance; subsequent columns referencing the same dataset reuse the
    parsed DataFrame to avoid repeated disk reads.
    """

    def __init__(self, seed: int = 42, logger=None, derive_key=None):
        super().__init__(seed, logger, derive_key=derive_key)
        # Cache parsed reference datasets across `apply()` calls so a single
        # pipeline doesn't reread the same CSV per masked column.
        self._cache: dict[str, pd.DataFrame] = {}

    def apply(self, column: pd.Series, rule: dict[str, Any]) -> pd.Series:
        column_name = rule.get('column', 'unnamed')
        ref_path = rule['reference']
        key_column = rule.get('key_column')

        ref_values = self._load_reference_values(ref_path, key_column)
        if not ref_values:
            self.logger.warning(
                f"Reference dataset '{ref_path}' is empty or unreadable; "
                f"leaving column '{column_name}' unchanged."
            )
            return column.copy()

        n = len(ref_values)
        column_key = self._column_key()
        seed = rule.get('seed', self.seed)

        if column_key is not None:
            self.logger.debug(
                f"Applying keyed reference lookup to column '{column_name}' "
                f"(reference={ref_path}, n={n})"
            )
            pick_idx = lambda s: _hmac_index(column_key, s, n)
        else:
            self.logger.debug(
                f"Applying seeded reference lookup to column '{column_name}' "
                f"(reference={ref_path}, n={n}, seed={seed}) — no master key configured"
            )
            pick_idx = lambda s: _seeded_index(s, seed, n)

        # The crypto itself (HMAC or SHA256 per value) can't be batched —
        # there's no whole-column hash function. So this isn't true
        # vectorization; we're just trimming the pandas overhead off the
        # per-row loop. Three things move out of the loop into single
        # whole-column ops: the null check (one C-level mask vs N Python
        # `pd.isna` calls), the string cast (one `.astype(str)` vs N
        # `str(val)` calls), and the pandas apply machinery itself (a plain
        # list comp is cheaper than `Series.apply`, which boxes/unboxes
        # every scalar). Worth ~3-6x over the legacy per-row apply.
        na_mask = column.isna()
        non_na_str = column[~na_mask].astype(str).tolist()
        picked = [ref_values[pick_idx(s)] for s in non_na_str]
        result = column.copy().astype(object)
        result.loc[~na_mask] = picked

        self._log_stats(column, result, rule)
        return result

    def validate_rule(self, rule: dict[str, Any]) -> None:
        super().validate_rule(rule)
        if 'reference' not in rule or not rule['reference']:
            raise ValueError(
                f"Rule for reference strategy is missing required 'reference' field "
                f"(column '{rule.get('column', 'unnamed')}')"
            )
        # `key_column` is optional — defaults to single-column references picking
        # the only column. Validate at load time when we know the dataset shape.

    # ── Internal helpers ──────────────────────────────────────────────

    def _load_reference_values(
        self, ref_path: str, key_column: str | None
    ) -> list[Any]:
        """Resolve `ref_path` to a list of pickable values. Cached per
        (path, key_column) pair so repeated columns don't re-read disk."""
        cache_key = f"{ref_path}::{key_column or ''}"
        if cache_key in self._cache:
            return self._cache[cache_key].tolist()

        path = Path(ref_path).expanduser()
        if not path.is_absolute():
            # Relative paths resolve against cwd today. If the caller wants a
            # different anchor (project root, dataset registry) it can be
            # added later as an engine-context option.
            path = Path(os.getcwd()) / path

        if not path.exists():
            raise ValueError(
                f"Reference dataset not found: {ref_path} "
                f"(resolved to {path})"
            )

        try:
            df = pd.read_csv(path)
        except Exception as exc:
            raise ValueError(
                f"Failed to read reference CSV '{path}': {exc}"
            ) from exc

        if df.empty:
            self.logger.warning(f"Reference dataset '{ref_path}' is empty.")
            self._cache[cache_key] = pd.Series(dtype=object)
            return []

        # Pick which column to draw from. Default to the only column in
        # single-column references (a CSV with no header is one column too —
        # pandas reads col0 as the value series).
        if key_column is None:
            if len(df.columns) > 1:
                self.logger.debug(
                    f"Reference '{ref_path}' has {len(df.columns)} columns; "
                    f"key_column unspecified — defaulting to first column "
                    f"'{df.columns[0]}'"
                )
            series = df.iloc[:, 0]
        else:
            if key_column not in df.columns:
                raise ValueError(
                    f"Reference column '{key_column}' not found in "
                    f"'{ref_path}' (available: {list(df.columns)})"
                )
            series = df[key_column]

        # Drop nulls — picking a NaN as a replacement value is never useful.
        series = series.dropna()
        self._cache[cache_key] = series
        return series.tolist()

    def _column_key(self) -> bytes | None:
        """Mirror HashStrategy._column_key — same `mask` info string so a
        column-level key set up once flows through every keyed strategy
        consistently."""
        if self.derive_key is None:
            return None
        try:
            return self.derive_key("mask")
        except Exception as exc:
            self.logger.warning(
                f"derive_key failed for 'mask' ({exc}); falling back to seeded reference"
            )
            return None


def _hmac_index(key: bytes, value: Any, n: int) -> int:
    """HMAC-SHA256(key, value) → integer index in [0, n). First 8 bytes of
    the digest is plenty of entropy for any reference size we care about."""
    msg = str(value).encode("utf-8", errors="replace")
    digest = hmac.new(key, msg, hashlib.sha256).digest()
    val = int.from_bytes(digest[:8], "big")
    return val % n


def _seeded_index(value: Any, seed: int, n: int) -> int:
    """SHA256(value + seed) → integer index in [0, n). Fallback when no
    master key is configured. Same input + same seed → same index."""
    msg = f"{value}{seed}".encode("utf-8", errors="replace")
    digest = hashlib.sha256(msg).digest()
    val = int.from_bytes(digest[:8], "big")
    return val % n

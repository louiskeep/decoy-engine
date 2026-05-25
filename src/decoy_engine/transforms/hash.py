"""
Hash masking strategy for the decoy_engine package.

Replaces values with deterministic hash strings. Two paths:
the keyed path (preferred) derives a per-column key via HKDF-SHA256
and then computes HMAC-SHA256(column_key, value) per cell; the
seeded fallback uses SHA-256(value + seed) for backward compatibility
with pre-keyed configs.

Pattern: HMAC-SHA256 with HKDF-SHA256 key derivation
  (HMAC RFC 2104; HKDF RFC 5869).
  HMAC: https://datatracker.ietf.org/doc/html/rfc2104
  HKDF: https://datatracker.ietf.org/doc/html/rfc5869
"""

from typing import Any

import pandas as pd

from decoy_engine.internal.crypto import deterministic_hash, hmac_hex
from decoy_engine.transforms.apply_context import ApplyContext
from decoy_engine.transforms.base import BaseMaskingStrategy

# ASCII Unit Separator: byte that does not appear in normal text data
# and is reserved by ASCII for exactly this kind of structured-field
# delimiter. Used to join (column_value, joint_col_1, joint_col_2, ...)
# into a single composite string for hashing. Picking a non-printable
# separator instead of (say) "|" reduces the chance a real data value
# could create a hash collision by happening to contain the separator.
_JOINT_SEP = "\x1f"


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

    def apply(self, column: pd.Series, rule: dict[str, Any]) -> pd.Series:
        """
        Replace values with deterministic hash strings that can't be reversed
        """
        column_name = rule.get("column", "unnamed")
        column_key = self._column_key(column_name)
        seed = rule.get("seed", self.seed)
        # Optional output-length cap. SHA-256 hex is 64 chars; legacy targets
        # with `CHAR(N)` columns can ask for a shorter slice. Truncation is
        # bitwise stable across runs because it slices a deterministic hash.
        truncate = self._resolve_truncate(rule.get("truncate"), column_name)

        if column_key is not None:
            self.logger.debug(f"Applying keyed hash to column '{column_name}'")
            hash_str = lambda s: hmac_hex(column_key, s)
        else:
            self.logger.debug(f"Applying legacy hash with seed {seed} (no master key configured)")
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

    def apply_with_context(
        self,
        column: pd.Series,
        rule: dict[str, Any],
        ctx: ApplyContext | None = None,
    ) -> pd.Series:
        """D5c joint-preservation entry point.

        When ctx.joint_columns is non-empty, hash each row as
        HMAC(column_key, value || SEP || joint_col_1_value || SEP || ...)
        instead of HMAC(column_key, value). The result:

          - Same (column, joints) tuple in source -> same hash in output.
            Joint frequency distribution is preserved exactly (the D5b
            shape-fidelity scorer should report ~1.0 on the joint).
          - Different joint values produce different hashes for the
            same column value, so an attacker who observes
            hashed_zip='abc123' cannot infer the source zip from
            single-column dictionary attacks alone.
          - Joint columns are read in SORTED NAME ORDER for output
            determinism (the dispatcher's dict ordering must not
            affect bytewise output).
          - Null source values pass through as null (preserves the
            null-passthrough invariant).
          - Null joint values are treated as empty strings; the
            separator byte keeps these from colliding with a real
            value that happens to start with a value at that position
            (e.g. joint='', other='X' vs joint='X', other='').

        When ctx is None or has no joint columns, delegates to
        single-column apply() so the existing keyed-FK-preservation
        property (same value across columns/tables -> same hash) is
        preserved.
        """
        joint_cols = ctx.joint_columns if ctx is not None else {}
        if not joint_cols:
            return self.apply(column, rule)

        column_name = rule.get("column", "unnamed")
        column_key = self._column_key(column_name)
        seed = rule.get("seed", self.seed)
        truncate = self._resolve_truncate(rule.get("truncate"), column_name)

        if column_key is not None:
            self.logger.debug(
                f"Applying keyed joint-hash to column '{column_name}' "
                f"with joints {sorted(joint_cols.keys())}",
            )

            def hash_str(s: str) -> str:
                return hmac_hex(column_key, s)
        else:
            self.logger.debug(
                f"Applying legacy joint-hash to column '{column_name}' "
                f"with joints {sorted(joint_cols.keys())} (seed {seed})",
            )

            def hash_str(s: str) -> str:
                return deterministic_hash(s, seed)

        # Sort joint names so the composite-string layout is independent
        # of the caller's dict ordering. Bitwise-stable output across
        # runs and Python versions.
        joint_names_sorted = sorted(joint_cols.keys())

        # Build the composite strings vectorized. The main column's
        # values come first; each joint column is appended after a
        # SEP byte with nulls coerced to empty strings.
        na_mask = column.isna()
        composites = column.astype(str)
        for name in joint_names_sorted:
            joint_series = joint_cols[name]
            composites = composites + _JOINT_SEP + joint_series.fillna("").astype(str)

        non_na_str = composites[~na_mask].tolist()
        if truncate:
            hashed = [hash_str(s)[:truncate] for s in non_na_str]
        else:
            hashed = [hash_str(s) for s in non_na_str]
        result = column.copy().astype(object)
        result.loc[~na_mask] = hashed

        self._log_stats(column, result, rule)
        return result

    def _resolve_truncate(self, raw, column_name: str) -> int | None:
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

    def _column_key(self, column_name: str) -> bytes | None:
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

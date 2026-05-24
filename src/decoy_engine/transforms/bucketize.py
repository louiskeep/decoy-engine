"""Bucketize generalization strategy.

Rounds numeric values into fixed-width buckets. Built for HIPAA Safe
Harbor age generalization (the rule that ages > 89 must be aggregated
into a single "90+" bucket plus the implicit "report by year of birth"
practice that gets generalized to decade buckets) and similar
generalization rules where less precision is the actual goal.

Output formats let the caller pick how to label the bucket:

  ``lower``    "20" — the lower bound; lossy but compact, good for FK-
                joinable surrogate keys.
  ``range``    "20-29" — explicit range, good for downstream displays.
  ``midpoint`` "25" — center of the bucket, good when downstream code
                expects a single representative value.

Negative values float back to their negative bucket (e.g. width=10,
value=-3 → -10..-1 bucket → "-10" / "-10--1" / "-5"). NaN / None pass
through unchanged.
"""

from typing import Any

import numpy as np
import pandas as pd

from decoy_engine.transforms.base import BaseMaskingStrategy

# Preset width shortcuts. Match the names in DISGUISES_GUIDE so a
# Disguise YAML can reference them directly.
#
# Naming convention: ``by_<unit>`` where <unit> is a singular noun or
# n-noun pair. The width is the literal integer the unit names, so
# operators can read the preset and immediately know the bucket size
# without consulting a table.
_PRESETS = {
    # Time-axis buckets (ages, tenure, year-of-birth, vintage).
    "by_year": 1,
    "by_2_years": 2,
    "by_5_years": 5,
    "by_decade": 10,
    "by_century": 100,
    # Currency-axis buckets (transaction amounts, balances, salaries).
    "by_thousand": 1_000,
    "by_ten_thousand": 10_000,
}

_FORMATS = {"lower", "range", "midpoint"}


class BucketizeStrategy(BaseMaskingStrategy):
    """Mask strategy that rounds numeric values into fixed-width buckets.

    Config:
      width:    int|float — required (or use ``preset``). Bucket width.
                Must be > 0.
      preset:   str — optional shortcut. See ``_PRESETS`` for the full
                list. Common picks: ``by_year`` (=1), ``by_5_years`` (=5),
                ``by_decade`` (=10), ``by_century`` (=100),
                ``by_thousand`` (=1000), ``by_ten_thousand`` (=10000).
                Overrides ``width`` if both set.
      format:   str — optional, default ``lower``. One of ``lower``,
                ``range``, ``midpoint``.
    """

    def apply(self, column: pd.Series, rule: dict[str, Any]) -> pd.Series:
        column_name = rule.get("column", "unnamed")
        width = self._resolve_width(rule, column_name)
        if width is None:
            return column

        fmt = str(rule.get("format", "lower")).lower()
        if fmt not in _FORMATS:
            self.logger.warning(
                f"bucketize.format={fmt!r} for column '{column_name}' is not "
                f"one of {sorted(_FORMATS)}; using 'lower'"
            )
            fmt = "lower"

        # Ints stay ints when width is integral so "20-29" doesn't print as
        # "20.0-29.0" — the common case for age / year buckets. Floats stay
        # floats so monetary buckets keep their precision.
        is_int_width = isinstance(width, int) and not isinstance(width, bool)

        self.logger.debug(
            f"Applying bucketize (width={width}, format={fmt}) to column '{column_name}'"
        )

        # Vectorized: numpy floor on the whole column + vectorized string
        # formatting. Non-numeric / NA values fall through to the original
        # value, matching the legacy per-row contract.
        nums = pd.to_numeric(column, errors="coerce")
        lower_f = np.floor(nums / width) * width

        if is_int_width:
            # Nullable Int64 so positions where `nums` is NaN survive the
            # int cast (becoming pd.NA, which formats correctly later).
            lower = lower_f.astype("Int64")
            upper_excl = lower + int(width)
        else:
            lower = lower_f
            upper_excl = lower + width

        if fmt == "lower":
            formatted = lower.astype(str)
        elif fmt == "range":
            # Inclusive upper for ints (so "20-29" not "20-30") — matches
            # the legacy `upper_excl - 1` convention.
            upper = upper_excl - 1 if is_int_width else upper_excl
            formatted = lower.astype(str) + "-" + upper.astype(str)
        else:  # midpoint
            mid = lower_f + width / 2
            if is_int_width and int(width) % 2 == 0:
                # Even integer width: half-step midpoint truncates to
                # int so labels stay compact ("25" not "25.0").
                mid = mid.astype("Int64")
            formatted = mid.astype(str)

        # Where the original was non-numeric or NaN, fall through to the
        # original value. Single Series.where call replaces the legacy
        # try/except per-row.
        result = formatted.where(nums.notna(), column)

        self._log_stats(column, result, rule)
        return result

    def _resolve_width(self, rule: dict[str, Any], column_name: str):
        preset = rule.get("preset")
        if preset is not None:
            if preset in _PRESETS:
                return _PRESETS[preset]
            self.logger.warning(
                f"bucketize.preset={preset!r} for column '{column_name}' is "
                f"not one of {sorted(_PRESETS)}; passing column through"
            )
            return None
        raw = rule.get("width")
        if raw is None:
            self.logger.warning(
                f"bucketize requires `width` or `preset` for column "
                f"'{column_name}'; passing column through unchanged"
            )
            return None
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            self.logger.warning(
                f"bucketize.width must be a number, got {raw!r} for column "
                f"'{column_name}'; passing column through unchanged"
            )
            return None
        if raw <= 0:
            self.logger.warning(
                f"bucketize.width={raw} for column '{column_name}' must be > 0; "
                f"passing column through unchanged"
            )
            return None
        return raw

    def validate_rule(self, rule: dict[str, Any]) -> None:
        super().validate_rule(rule)
        if "width" not in rule and "preset" not in rule:
            self.logger.debug(
                f"bucketize on column '{rule['column']}' has neither `width` "
                f"nor `preset` set — column will pass through unchanged"
            )

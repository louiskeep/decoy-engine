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

import math
import pandas as pd
from typing import Dict, Any, Optional

from decoy_engine.transforms.base import BaseMaskingStrategy


# Preset width shortcuts. Match the names in DISGUISES_GUIDE so a
# Disguise YAML can reference them directly.
_PRESETS = {
    'by_decade': 10,
    'by_5_years': 5,
}

_FORMATS = {'lower', 'range', 'midpoint'}


class BucketizeStrategy(BaseMaskingStrategy):
    """Mask strategy that rounds numeric values into fixed-width buckets.

    Config:
      width:    int|float — required (or use ``preset``). Bucket width.
                Must be > 0.
      preset:   str — optional shortcut. ``by_decade`` (=10) or
                ``by_5_years`` (=5). Overrides ``width`` if both set.
      format:   str — optional, default ``lower``. One of ``lower``,
                ``range``, ``midpoint``.
    """

    def apply(self, column: pd.Series, rule: Dict[str, Any]) -> pd.Series:
        column_name = rule.get('column', 'unnamed')
        width = self._resolve_width(rule, column_name)
        if width is None:
            return column

        fmt = str(rule.get('format', 'lower')).lower()
        if fmt not in _FORMATS:
            self.logger.warning(
                f"bucketize.format={fmt!r} for column '{column_name}' is not "
                f"one of {sorted(_FORMATS)}; using 'lower'"
            )
            fmt = 'lower'

        # Ints stay ints when width is integral so "20-29" doesn't print as
        # "20.0-29.0" — the common case for age / year buckets. Floats stay
        # floats so monetary buckets keep their precision.
        is_int_width = isinstance(width, int) and not isinstance(width, bool)

        self.logger.debug(
            f"Applying bucketize (width={width}, format={fmt}) to column "
            f"'{column_name}'"
        )

        def bucket_value(val):
            if val is None or pd.isna(val):
                return val
            try:
                v = float(val)
            except (TypeError, ValueError):
                # Non-numeric input — leave unchanged. Logged at the call
                # site is too noisy; the missing values surface as
                # passthrough in downstream stats.
                return val
            lower = math.floor(v / width) * width
            upper_excl = lower + width
            if is_int_width:
                lower = int(lower)
                upper_excl = int(upper_excl)
            if fmt == 'lower':
                return str(lower)
            if fmt == 'range':
                # Inclusive upper for ints (so "20-29" not "20-30").
                upper = upper_excl - 1 if is_int_width else upper_excl
                return f"{lower}-{upper}"
            # midpoint
            mid = lower + width / 2
            if is_int_width and width % 2 == 0:
                # Even integer width gives a half-step midpoint; truncate
                # to int so labels stay compact.
                return str(int(mid))
            return str(mid)

        result = column.apply(bucket_value)
        self._log_stats(column, result, rule)
        return result

    def _resolve_width(self, rule: Dict[str, Any], column_name: str):
        preset = rule.get('preset')
        if preset is not None:
            if preset in _PRESETS:
                return _PRESETS[preset]
            self.logger.warning(
                f"bucketize.preset={preset!r} for column '{column_name}' is "
                f"not one of {sorted(_PRESETS)}; passing column through"
            )
            return None
        raw = rule.get('width')
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

    def validate_rule(self, rule: Dict[str, Any]) -> None:
        super().validate_rule(rule)
        if 'width' not in rule and 'preset' not in rule:
            self.logger.debug(
                f"bucketize on column '{rule['column']}' has neither `width` "
                f"nor `preset` set — column will pass through unchanged"
            )

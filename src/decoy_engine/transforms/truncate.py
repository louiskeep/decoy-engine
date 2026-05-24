"""Truncate generalization strategy.

Keeps the first N characters of each value, dropping the rest. Built for
HIPAA Safe Harbor §164.514(b)(2)(i)(B) — geographic generalization
("the initial three digits of a ZIP code") — and similar rules where
the right answer is "less precision," not "synthetic value."

Idempotent and order-preserving (truncating the same input twice yields
the same prefix), so FK joins on the truncated column behave the way
joins on a real ZIP3 column do — many-to-one mapping rather than the
one-to-one identity that masking strategies normally guarantee.
"""

from typing import Any

import pandas as pd

from decoy_engine.transforms.base import BaseMaskingStrategy


class TruncateStrategy(BaseMaskingStrategy):
    """Mask strategy that keeps the first N characters of each value.

    Config:
      length:    int — required. Number of chars to keep. Must be >= 1.
      from_end:  bool — optional, default False. When True, keeps the
                 *last* N chars instead of the first (useful for
                 last-4 of a card / SSN style affordances).
    """

    def apply(self, column: pd.Series, rule: dict[str, Any]) -> pd.Series:
        column_name = rule.get("column", "unnamed")
        length = self._resolve_length(rule.get("length"), column_name)
        if length is None:
            # Invalid config — log and pass through. Don't raise; one bad
            # rule shouldn't abort the whole masking run.
            return column
        from_end = bool(rule.get("from_end", False))

        self.logger.debug(
            f"Applying truncate (length={length}, from_end={from_end}) to column '{column_name}'"
        )

        # Vectorized: pandas' StringMethods does C-level slicing for the
        # whole column in one pass. We slice only the non-NA values so
        # NaN positions stay NaN (the str-cast would turn them into the
        # string "nan" otherwise).
        na_mask = column.isna()
        result = column.copy().astype(object)
        non_na_str = column[~na_mask].astype(str)
        if from_end:
            sliced = non_na_str.str[-length:]
        else:
            sliced = non_na_str.str[:length]
        result.loc[~na_mask] = sliced
        self._log_stats(column, result, rule)
        return result

    def _resolve_length(self, raw, column_name: str) -> int | None:
        """Coerce + validate the `length` config. None / 0 / non-int values
        are treated as "no truncate" with a warning rather than raised so
        the run keeps going on a single bad rule."""
        if raw is None:
            self.logger.warning(
                f"truncate.length is required for column '{column_name}'; "
                f"passing column through unchanged"
            )
            return None
        # Reject bool first because Python's `bool` is a subclass of `int`.
        if isinstance(raw, bool) or not isinstance(raw, int):
            self.logger.warning(
                f"truncate.length must be an integer, got {raw!r} for column "
                f"'{column_name}'; passing column through unchanged"
            )
            return None
        if raw < 1:
            self.logger.warning(
                f"truncate.length={raw} for column '{column_name}' must be >= 1; "
                f"passing column through unchanged"
            )
            return None
        return raw

    def validate_rule(self, rule: dict[str, Any]) -> None:
        super().validate_rule(rule)
        if "length" not in rule:
            self.logger.debug(
                f"truncate strategy on column '{rule['column']}' has no "
                f"`length` set — column will pass through unchanged"
            )

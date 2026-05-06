import hashlib
import hmac
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import pandas as pd

from decoy_engine.transforms.base import BaseMaskingStrategy

_COMMON_FORMATS = [
    '%Y-%m-%d',
    '%m/%d/%Y',
    '%d/%m/%Y',
    '%Y/%m/%d',
    '%m-%d-%Y',
    '%d-%m-%Y',
    '%Y%m%d',
    '%m/%d/%y',
    '%d/%m/%y',
    '%Y-%m-%dT%H:%M:%S',
    '%Y-%m-%d %H:%M:%S',
]


def _parse_date(val: str, date_format: Optional[str]) -> Optional[datetime]:
    if date_format:
        try:
            return datetime.strptime(val, date_format)
        except ValueError:
            return None
    for fmt in _COMMON_FORMATS:
        try:
            return datetime.strptime(val, fmt)
        except ValueError:
            continue
    return None


def _detect_format(series: pd.Series) -> Optional[str]:
    sample = series.dropna().astype(str).head(20)
    for fmt in _COMMON_FORMATS:
        try:
            for v in sample:
                datetime.strptime(v.strip(), fmt)
            return fmt
        except ValueError:
            continue
    return None


def _shift_for_value_md5(val: str, min_days: int, max_days: int) -> int:
    """Legacy per-value shift derived from MD5 hash (no key)."""
    range_size = max_days - min_days + 1
    h = int(hashlib.md5(val.encode('utf-8', errors='replace')).hexdigest(), 16)
    return min_days + (h % range_size)


def _shift_for_value_keyed(
    key: bytes, val: str, min_days: int, max_days: int
) -> int:
    """Keyed per-value shift via HMAC-SHA256(column_key, val).
    Same value + same key → same shift, but the shift amount is not
    derivable from the value alone (unlike the legacy MD5 path).
    """
    range_size = max_days - min_days + 1
    digest = hmac.new(
        key, val.encode('utf-8', errors='replace'), hashlib.sha256
    ).digest()
    h = int.from_bytes(digest[:8], 'big')
    return min_days + (h % range_size)


class DateShiftStrategy(BaseMaskingStrategy):
    """
    Shifts date values by a deterministic per-value offset.

    Two paths:
      * **Keyed (preferred).** HMAC-SHA256(column_key, value) → shift days.
        Cross-run, cross-instance stable; not derivable from value alone.
      * **Legacy.** MD5(value) → shift days. Cross-run stable per-input but
        derivable. Kept as fallback when no master key is configured.

    The output format matches the input format unless ``date_format`` is set.
    """

    def __init__(self, seed: int = 42, logger=None, derive_key=None):
        super().__init__(seed, logger, derive_key=derive_key)
        self.strategy_name = 'date_shift'

    def apply(self, column: pd.Series, rule: Dict[str, Any]) -> pd.Series:
        min_days = int(rule.get('min_days', -365))
        max_days = int(rule.get('max_days', 365))
        date_format: Optional[str] = rule.get('date_format') or None
        column_name = rule.get('column', 'unnamed')
        column_key = self._column_key(column_name)

        if min_days > max_days:
            min_days, max_days = max_days, min_days

        fmt = date_format or _detect_format(column)

        if column_key is not None:
            self.logger.debug(
                f"Applying keyed date_shift to column '{column_name}'"
            )
            shift_fn = lambda s: _shift_for_value_keyed(column_key, s, min_days, max_days)
        else:
            self.logger.debug(
                f"Applying legacy date_shift (MD5) to column '{column_name}'"
            )
            shift_fn = lambda s: _shift_for_value_md5(s, min_days, max_days)

        def shift_val(val):
            if val is None or pd.isna(val):
                return val
            s = str(val).strip()
            dt = _parse_date(s, fmt)
            if dt is None:
                self.logger.warning(
                    f"date_shift: could not parse '{s}' — leaving unchanged"
                )
                return val
            delta = shift_fn(s)
            shifted = dt + timedelta(days=delta)
            out_fmt = fmt or '%Y-%m-%d'
            return shifted.strftime(out_fmt)

        result = column.apply(shift_val)
        self._log_stats(column, result, rule)
        return result

    def _column_key(self, column_name: str) -> Optional[bytes]:
        if self.derive_key is None:
            return None
        try:
            return self.derive_key(f"col:{column_name}")
        except Exception as exc:
            self.logger.warning(
                f"derive_key failed for col:{column_name} ({exc}); falling back to legacy MD5"
            )
            return None

    def validate_rule(self, rule: Dict[str, Any]) -> None:
        if 'column' not in rule:
            raise ValueError("date_shift rule is missing 'column' field")

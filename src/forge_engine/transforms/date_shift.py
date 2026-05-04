import hashlib
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import pandas as pd

from forge_engine.transforms.base import BaseMaskingStrategy

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


def _shift_for_value(val: str, min_days: int, max_days: int) -> int:
    """Deterministic per-value shift derived from MD5 hash."""
    range_size = max_days - min_days + 1
    h = int(hashlib.md5(val.encode('utf-8', errors='replace')).hexdigest(), 16)
    return min_days + (h % range_size)


class DateShiftStrategy(BaseMaskingStrategy):
    """
    Shifts date values by a deterministic per-value offset.

    The shift amount is derived from an MD5 hash of the original value, so
    the same date always shifts by the same number of days (cross-run consistent).
    The output format matches the input format unless date_format is set.
    """

    def __init__(self, seed: int = 42, logger=None):
        super().__init__(seed, logger)
        self.strategy_name = 'date_shift'

    def apply(self, column: pd.Series, rule: Dict[str, Any]) -> pd.Series:
        min_days = int(rule.get('min_days', -365))
        max_days = int(rule.get('max_days', 365))
        date_format: Optional[str] = rule.get('date_format') or None

        if min_days > max_days:
            min_days, max_days = max_days, min_days

        fmt = date_format or _detect_format(column)

        def shift_val(val):
            if val is None or pd.isna(val):
                return val
            s = str(val).strip()
            dt = _parse_date(s, fmt)
            if dt is None:
                self.logger.warning(f"date_shift: could not parse '{s}' — leaving unchanged")
                return val
            delta = _shift_for_value(s, min_days, max_days)
            shifted = dt + timedelta(days=delta)
            out_fmt = fmt or '%Y-%m-%d'
            return shifted.strftime(out_fmt)

        result = column.apply(shift_val)
        self._log_stats(column, result, rule)
        return result

    def validate_rule(self, rule: Dict[str, Any]) -> None:
        if 'column' not in rule:
            raise ValueError("date_shift rule is missing 'column' field")

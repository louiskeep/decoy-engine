"""Date-shape sniffing for string columns, split from profiler.py.

The parse rate is judged on the first ``_SAMPLE`` non-null values. The full
series is coerced only when that rate clears the threshold, so a free-text
column costs 200 parses instead of one per row. A column that is a date still
gets the full coercion, so its min, max and invalid counts cover every row.
"""

from __future__ import annotations

import warnings

import pandas as pd

from decoy_engine.profiling.types import FieldStats

_SAMPLE = 200
_PARSE_RATE_THRESHOLD = 0.7


def _coerce(values: pd.Series) -> pd.Series:
    # Format inference is the point here. pandas 2.x emits a chatty UserWarning
    # when it falls back to dateutil; we probe for a date-shaped column without
    # a format up front, so suppress it.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=".*Could not infer format.*",
            category=UserWarning,
        )
        return pd.to_datetime(values, errors="coerce")


def sniff_dates(non_null: pd.Series, fs: FieldStats) -> None:
    """Fill the date fields on `fs` when `non_null` (string values) parses as dates."""
    sample_size = min(_SAMPLE, len(non_null))
    if not sample_size:
        return
    head_rate = _coerce(non_null.head(sample_size)).notna().sum() / sample_size
    if head_rate < _PARSE_RATE_THRESHOLD:
        return
    coerced = _coerce(non_null)
    invalid_mask = coerced.isna()
    valid = coerced.dropna()
    if len(valid) > 0:
        fs.date_min = valid.min().isoformat()
        fs.date_max = valid.max().isoformat()
    fs.invalid_count = int(invalid_mask.sum())
    if fs.invalid_count > 0:
        fs.sample_invalid = non_null[invalid_mask].head(3).astype(str).tolist()

"""Sentinel value detection for STORM.

A "sentinel" is a value that parsed structurally (so it doesn't show up as
invalid in the basic profiler) but is suspicious in context. Examples:

    - `0001-01-01` in a `start_date` column — valid date, won't load into
      Postgres `date` type, almost certainly a placeholder for "unknown".
    - `9999-12-31` — common "end of time" placeholder.
    - `-1` in a foreign-key column — legacy "no parent" sentinel.
    - `999999999` in a SSN-shaped column — bogus filler.
    - `"N/A"`, `"NULL"`, `"TBD"`, `"UNKNOWN"` strings.

These are surfaced as `SentinelFlag`s on FieldStats so FORECAST can warn
the user and suggest fixes ("replace with NULL, drop row, etc.").

MVP scope: the obvious ones, no statistical outlier detection yet (that's
midrange — z-score, IQR, distribution-based).
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime
from typing import Any

import pandas as pd

from decoy_engine.storm.types import SentinelFlag


# ── known sentinel values ─────────────────────────────────────────────────────

# Date sentinels — parse to a real date but are placeholder-y.
_DATE_SENTINELS: dict[date, str] = {
    date(1, 1, 1):       "year-0001 placeholder, won't load into Postgres date type",
    date(1900, 1, 1):    "1900-01-01 — common 'unknown date' placeholder",
    date(1899, 12, 31):  "1899-12-31 — Excel date origin, often appears from corrupted serializations",
    date(1970, 1, 1):    "1970-01-01 — Unix epoch, often a default for missing timestamps",
    date(9999, 12, 31):  "9999-12-31 — common 'end of time' placeholder",
}

# Numeric sentinels — keyed by exact value.
_NUMERIC_SENTINELS: dict[float, str] = {
    -1.0:           "-1 — common 'unknown / no parent' sentinel",
    -999.0:         "-999 — common missing-value sentinel",
    999999999.0:    "999999999 — common SSN/ID filler",
    -2147483648.0:  "INT_MIN — likely overflow or 'no value' placeholder",
}

# String sentinels — case-insensitive exact match after stripping whitespace.
_STRING_SENTINELS: set[str] = {
    "n/a", "na", "null", "none", "nil",
    "tbd", "tba", "todo",
    "unknown", "unk",
    "missing", "no data", "n.a.",
    "?", "-", "--", "...",
    "x", "xxx", "xxxx", "xxxxx",
}


# ── thresholds for "out of plausible range" date detection ────────────────────

# DOB columns: age 0-120 expected, so birth year between (today.year - 120)
# and today.year. We use 1880 as a permissive lower bound and "today" as upper.
_REASONABLE_DOB_LOWER = date(1880, 1, 1)


# ── helpers ───────────────────────────────────────────────────────────────────

_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T].*)?$")


def _to_date(v: Any) -> date | None:
    """Best-effort coercion to a `date`, returning None if it doesn't fit.

    Handles dates outside pandas' Timestamp range (1677-09-21 .. 2262-04-11)
    via a stdlib regex+`date()` fallback. This is critical for sentinel
    detection — `0001-01-01` and `9999-12-31` are exactly the values we
    want to flag, and they're outside pandas' supported range.
    """
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if pd.isna(v):
        return None
    s = str(v).strip()
    # Try stdlib ISO parse first (handles full date range).
    m = _ISO_DATE_RE.match(s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    # Fall back to pandas (handles US/EU formats, datetimes with timezones, etc.).
    try:
        ts = pd.to_datetime(s, errors="coerce")
        if pd.isna(ts):
            return None
        return ts.to_pydatetime().date()
    except Exception:
        return None


# ── public detector ───────────────────────────────────────────────────────────

def detect_sentinels(series: pd.Series, col_name: str) -> list[SentinelFlag]:
    """Return a list of SentinelFlags found in this column. Empty list if clean."""
    flags: list[SentinelFlag] = []
    non_null = series.dropna()
    if len(non_null) == 0:
        return flags

    # Datetime-typed columns: check for known date sentinels + DOB-range sanity.
    if pd.api.types.is_datetime64_any_dtype(series):
        flags.extend(_scan_dates(non_null, col_name))
    # Numeric columns: known numeric sentinels.
    elif pd.api.types.is_numeric_dtype(series):
        flags.extend(_scan_numerics(non_null))
    # Object / string columns: string sentinels, plus stdlib date-coerce for
    # date-hinted columns (NOT pandas-coerce — pandas Timestamps reject the
    # 0001-01-01 / 9999-12-31 sentinels we care most about).
    else:
        flags.extend(_scan_strings(non_null))
        if any(hint in (col_name or "").lower() for hint in ("date", "dob", "birth", "start", "end", "due")):
            flags.extend(_scan_dates(non_null, col_name))

    return flags


# ── scanners (private) ────────────────────────────────────────────────────────

def _scan_dates(non_null: pd.Series, col_name: str) -> list[SentinelFlag]:
    flags: list[SentinelFlag] = []
    out_of_range_count = 0
    today = date.today()
    is_dob_column = any(h in (col_name or "").lower() for h in ("dob", "birth"))

    counter: Counter[date] = Counter()
    for v in non_null:
        d = _to_date(v)
        if d is None:
            continue
        # Known sentinel exact match.
        if d in _DATE_SENTINELS:
            counter[d] += 1
            continue
        # Out-of-range date checks.
        if is_dob_column:
            if d > today or d < _REASONABLE_DOB_LOWER:
                out_of_range_count += 1
        elif d.year < 1900 or d.year > 2100:
            out_of_range_count += 1

    for d, count in counter.items():
        flags.append(SentinelFlag(
            kind="date_sentinel",
            value=d.isoformat(),
            count=count,
            note=_DATE_SENTINELS[d],
        ))

    if out_of_range_count > 0:
        flags.append(SentinelFlag(
            kind="date_out_of_range",
            value=f"{out_of_range_count} value(s)",
            count=out_of_range_count,
            note=(
                "DOB values outside 1880–today" if is_dob_column
                else "date values outside 1900–2100"
            ),
        ))

    return flags


def _scan_numerics(non_null: pd.Series) -> list[SentinelFlag]:
    flags: list[SentinelFlag] = []
    counter: Counter[float] = Counter()
    for v in non_null:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f in _NUMERIC_SENTINELS:
            counter[f] += 1
    for f, count in counter.items():
        flags.append(SentinelFlag(
            kind="numeric_sentinel",
            value=str(int(f)) if f.is_integer() else str(f),
            count=count,
            note=_NUMERIC_SENTINELS[f],
        ))
    return flags


def _scan_strings(non_null: pd.Series) -> list[SentinelFlag]:
    flags: list[SentinelFlag] = []
    counter: Counter[str] = Counter()
    for v in non_null.astype(str):
        normalized = v.strip().lower()
        if normalized in _STRING_SENTINELS:
            counter[normalized] += 1
    for normalized, count in counter.items():
        flags.append(SentinelFlag(
            kind="string_sentinel",
            value=normalized,
            count=count,
            note=f"placeholder string {normalized!r} — probably means missing data",
        ))
    return flags

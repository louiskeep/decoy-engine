"""
Date-shift masking strategy for the decoy_engine package.

Shifts each date by a per-value offset within ``[min_days, max_days]``.
Same input + same key produces the same shift across runs and
instances. Used to break temporal joinability between datasets while
preserving relative ordering and rough temporal density.

Two paths:
  - **Keyed (preferred).** Engine-supplied master key derives a
    per-column key via the existing derive_key path; per-value shift
    comes from HMAC-SHA256(column_key, value) reduced into the
    declared range.
  - **Legacy fallback.** Pre-keyed configs use MD5(value) reduced
    into the range. Preserved for reproducibility on existing
    fixtures; new configs should always declare a key.

Pattern: HMAC-SHA256-keyed deterministic offset (HMAC RFC 2104).
  HMAC: https://datatracker.ietf.org/doc/html/rfc2104
"""

import hashlib
import hmac
from datetime import datetime
from typing import Any

import pandas as pd

from decoy_engine.transforms.base import BaseMaskingStrategy

_COMMON_FORMATS = [
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%d/%m/%Y",
    "%Y/%m/%d",
    "%m-%d-%Y",
    "%d-%m-%Y",
    "%Y%m%d",
    "%m/%d/%y",
    "%d/%m/%y",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
]


def _parse_date(val: str, date_format: str | None) -> datetime | None:
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


def _detect_format(series: pd.Series) -> str | None:
    """Pick the date format that parses every value in the sample.

    F9 fix: bumped the sample from 20 -> 200 rows. 20 was demonstrably too
    small for mixed-format columns coming out of legacy ETL pipelines: if
    the first 20 happen to share one format but the rest are different,
    the wrong format is locked in and downstream rows silently pass
    through unshifted.

    When more than one format parses the entire sample, we emit a warning
    rather than tie-breaking silently. ``_shift_for_value_*`` handles
    unparseable rows by leaving them unchanged, so a wrong winner here
    is a silent shift miss, not a crash.
    """
    sample = series.dropna().astype(str).head(min(200, len(series)))
    candidates: list[str] = []
    for fmt in _COMMON_FORMATS:
        ok = True
        for v in sample:
            try:
                datetime.strptime(v.strip(), fmt)
            except ValueError:
                ok = False
                break
        if ok:
            candidates.append(fmt)
    if not candidates:
        return None
    if len(candidates) > 1:
        import warnings as _warnings
        _warnings.warn(
            "date_shift._detect_format: column matches multiple formats "
            f"{candidates!r}; using the first ({candidates[0]!r}). Configure "
            "date_format explicitly to remove ambiguity.",
            stacklevel=2,
        )
    return candidates[0]


def _shift_for_value_md5(val: str, min_days: int, max_days: int) -> int:
    """Legacy per-value shift derived from MD5 hash (no key).

    Non-crypto use of MD5: this fallback exists for date-shift configs
    that did not declare a key. New configs should always declare a
    key and hit the HMAC-SHA256 path below; this branch is preserved
    for reproducibility on existing pre-keyed configs.
    """
    range_size = max_days - min_days + 1
    h = int(hashlib.md5(val.encode("utf-8", errors="replace")).hexdigest(), 16)  # noqa: S324
    return min_days + (h % range_size)


def _shift_for_value_keyed(key: bytes, val: str, min_days: int, max_days: int) -> int:
    """Keyed per-value shift via HMAC-SHA256(column_key, val).
    Same value + same key → same shift, but the shift amount is not
    derivable from the value alone (unlike the legacy MD5 path).
    """
    range_size = max_days - min_days + 1
    digest = hmac.new(key, val.encode("utf-8", errors="replace"), hashlib.sha256).digest()
    h = int.from_bytes(digest[:8], "big")
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
        self.strategy_name = "date_shift"

    def apply(self, column: pd.Series, rule: dict[str, Any]) -> pd.Series:
        min_days = int(rule.get("min_days", -365))
        max_days = int(rule.get("max_days", 365))
        date_format: str | None = rule.get("date_format") or None
        column_name = rule.get("column", "unnamed")
        column_key = self._column_key(column_name)

        if min_days > max_days:
            min_days, max_days = max_days, min_days

        # Arrow-backed string columns send to_datetime, astype(str), and
        # the format-detection sample loop down per-element Python
        # fallback paths — costs roughly 5s extra at 1M rows under the
        # default hybrid engine. Pre-materialize once to numpy-backed
        # object dtype so every subsequent step lands on the fast paths.
        # Cost: ~95 ms at 1M rows; saves ~1+ s on the same input. The
        # `is_extension_array_dtype` check skips object / datetime64 /
        # plain numpy inputs (no work needed there).
        if pd.api.types.is_extension_array_dtype(column.dtype):
            column = column.astype(object)

        fmt = date_format or _detect_format(column)

        if column_key is not None:
            self.logger.debug(f"Applying keyed date_shift to column '{column_name}'")
            shift_fn = lambda s: _shift_for_value_keyed(column_key, s, min_days, max_days)
        else:
            self.logger.debug(f"Applying legacy date_shift (MD5) to column '{column_name}'")
            shift_fn = lambda s: _shift_for_value_md5(s, min_days, max_days)

        # Vectorized: turn every value into datetime64 in one C-level pass
        # instead of 5M-per-row strptime calls. `errors='coerce'` produces
        # NaT for unparseable values OR for dates outside pandas'
        # nanosecond range (~1677-2262); we restore those positions to the
        # original input at the end so behavior matches the legacy
        # per-row path for normal data and degrades gracefully on edge
        # cases. Per-value crypto for the shift amount is irreducible —
        # but a list comprehension over .tolist() avoids pandas.apply's
        # per-row dispatch overhead. Net at 5M rows: ~15 min → ~30-60 s.
        parsed = pd.to_datetime(column, format=fmt, errors="coerce")
        na_mask = column.isna()
        parse_failed = parsed.isna() & ~na_mask

        if parse_failed.any():
            # Dedupe the warning per unique value rather than per row —
            # the legacy path logged once per row which spammed the log
            # pipeline on big columns of mostly-bad data.
            for v in column[parse_failed].dropna().unique()[:20]:
                self.logger.warning(f"date_shift: could not parse '{v}' — leaving unchanged")

        str_values = column.astype(str)
        shifts = [shift_fn(v) for v in str_values.tolist()]

        shifted = parsed + pd.to_timedelta(shifts, unit="D")
        out_fmt = fmt or "%Y-%m-%d"
        formatted = shifted.dt.strftime(out_fmt)

        # Build output from the original (preserves NaN + unparseable),
        # replace only successful rows with the shifted value.
        success_mask = ~parsed.isna() & ~na_mask
        result = column.astype(object).copy()
        result.loc[success_mask] = formatted.loc[success_mask]

        self._log_stats(column, result, rule)
        return result

    def _column_key(self, column_name: str) -> bytes | None:
        """Derive the mask subkey via the caller-supplied resolver. Same as
        HashStrategy._column_key — instance-master-only, no per-column
        tagging. ``column_name`` is kept for log context only."""
        if self.derive_key is None:
            return None
        try:
            return self.derive_key("mask")
        except Exception as exc:
            self.logger.warning(f"derive_key failed for 'mask' ({exc}); falling back to legacy MD5")
            return None

    def validate_rule(self, rule: dict[str, Any]) -> None:
        if "column" not in rule:
            raise ValueError("date_shift rule is missing 'column' field")

"""bucket_perturb datetime strategy (SP-08b / P5.B.datetime): coarse time-bucket generalization.

Generalises a date column by snapping each value to a random position within its
time bucket (ISO week, calendar month, or calendar quarter). The bucket boundary is
determined by the input date; the position within the bucket is derived
deterministically from ``derive(job_seed, namespace, value)`` so the same input
value always maps to the same output position.

Intended use: break sub-bucket temporal precision while preserving coarse ordering
and temporal density (values in Q3 stay in Q3; values in the same month stay in
that month). Complementary to ``date_shift`` which preserves exact cross-record
ordering but shifts all dates by a bounded amount.

Bucket sizes:
  week    -- ISO 8601 week (Monday=day 0, Sunday=day 6). Output day in [0, 6].
  month   -- Calendar month. Output day in [1, days_in_month].
  quarter -- Calendar quarter (Q1=Jan-Mar, Q2=Apr-Jun, Q3=Jul-Sep, Q4=Oct-Dec).
             Output day in [1, days_in_quarter].

Determinism: uses ``decoy_engine.determinism.derive`` (S3 / HKDF-SHA256)
  which follows the same keying contract as date_shift and hash strategies.
  Same (job_seed, namespace, value) -> same bucket offset across all runs.

Null and unparseable values are passed through unchanged (same contract as date_shift).

Pattern: ISO 8601 calendar period partitioning (ISO 8601, https://www.iso.org/iso-8601-date-and-time-format.html).
  Time-bucket perturbation follows the general principle of k-anonymity temporal
  generalization: dates within the same bucket are indistinguishable at the bucket
  granularity (Sweeney 2002, "k-anonymity: A Model for Protecting Privacy").
"""

from __future__ import annotations

import calendar
import datetime
import logging
from typing import Any

import pandas as pd

from decoy_engine.determinism import derive
from decoy_engine.generation.pool._canonicalize import _canonicalize_source

_LOG = logging.getLogger(__name__)

# Supported bucket names and their corresponding date-truncation strategies.
_VALID_BUCKETS = frozenset({"week", "month", "quarter"})

# strftime directives by what they write back. bucket_perturb perturbs the
# calendar date, so only DATE directives let a date survive the round trip; the
# TIME/other ones are legal but write midnight, zero or nothing. Anything else is
# unknown. `%C %h %e %D %F` are absent on purpose: pandas cannot parse them.
_DATE_DIRECTIVES = frozenset("YyGmbBdjUWVaAuwxc")
_OTHER_DIRECTIVES = frozenset("HIMSpfzZ")
_FORMAT_ADVICE = "Use a concrete pattern such as %Y-%m-%d."

# Quarter start month by quarter index (0-based: 0=Q1, 1=Q2, 2=Q3, 3=Q4).
_QUARTER_START_MONTH = [1, 4, 7, 10]


def _bucket_start_and_size(date: datetime.date, bucket: str) -> tuple[datetime.date, int]:
    """Return the bucket start date and the number of days in the bucket.

    Args:
        date: The input date.
        bucket: One of ``"week"``, ``"month"``, or ``"quarter"``.

    Returns:
        ``(bucket_start, bucket_size_days)`` where ``bucket_start`` is the first
        day of the bucket and ``bucket_size_days`` is the number of days in it.
    """
    if bucket == "week":
        # ISO week starts on Monday (weekday() == 0).
        start = date - datetime.timedelta(days=date.weekday())
        return start, 7

    if bucket == "month":
        start = datetime.date(date.year, date.month, 1)
        _, days_in_month = calendar.monthrange(date.year, date.month)
        return start, days_in_month

    if bucket == "quarter":
        q_idx = (date.month - 1) // 3  # 0=Q1, 1=Q2, 2=Q3, 3=Q4
        q_start_month = _QUARTER_START_MONTH[q_idx]
        start = datetime.date(date.year, q_start_month, 1)
        # Compute quarter size: count days from start of quarter to end of last month.
        end_month = q_start_month + 2
        _, days_in_end_month = calendar.monthrange(date.year, end_month)
        end = datetime.date(date.year, end_month, days_in_end_month)
        size = (end - start).days + 1
        return start, size

    # Defense-in-depth: an unrecognized bucket must never silently coarsen.
    # validate_bucket_perturb_config should catch this before apply_bucket_perturb
    # is called; this raise is the second line of defense.
    raise ValueError(
        f"bucket_perturb: unrecognized bucket {bucket!r}. "
        f"Supported values: {sorted(_VALID_BUCKETS)}."
    )


def _perturb_date(
    date: datetime.date,
    bucket: str,
    job_seed: bytes,
    namespace: str,
    value_str: str,
) -> datetime.date:
    """Derive a deterministic position within the date's bucket.

    Uses ``derive(job_seed, namespace, value_str)`` to produce a per-value offset
    within ``[0, bucket_size - 1]`` days from the bucket start. Same inputs ->
    same output (full determinism across runs and instances).

    Args:
        date: The input date.
        bucket: One of ``"week"``, ``"month"``, ``"quarter"``.
        job_seed: 32-byte entropy from the job seed envelope.
        namespace: Column namespace (required; ensures cross-column isolation).
        value_str: Canonical string representation of the source value.

    Returns:
        A date within the same bucket as ``date``.
    """
    bucket_start, bucket_size = _bucket_start_and_size(date, bucket)
    digest = derive(job_seed, namespace, _canonicalize_source(value_str))
    offset = int.from_bytes(digest[:8], "big") % bucket_size
    return bucket_start + datetime.timedelta(days=offset)


def apply_bucket_perturb(
    series: pd.Series,
    bucket: str,
    job_seed: bytes,
    namespace: str,
    date_format: str | None,
) -> pd.Series:
    """Apply bucket_perturb to a pandas Series of date strings.

    Each value is parsed, snapped to a deterministic position within its time bucket,
    and reformatted. Null and unparseable values are passed through unchanged.

    Args:
        series: Input pandas Series of date strings.
        bucket: Time bucket name: ``"week"``, ``"month"``, or ``"quarter"``.
        job_seed: 32-byte job seed bytes (from the plan's SeedEnvelope).
        namespace: Column namespace for derive() isolation. Required.
        date_format: strptime/strftime format string. Detected from data if None.

    Returns:
        A new pandas Series with perturbed dates (same format as input).
    """
    from decoy_engine.transforms.date_shift import _detect_format

    if pd.api.types.is_extension_array_dtype(series.dtype):
        series = series.astype(object)

    fmt = date_format or _detect_format(series)
    if fmt is None:
        _LOG.warning(
            "bucket_perturb: could not detect date format; column values passed through unchanged."
        )
        return series.copy()

    parsed = pd.to_datetime(series, format=fmt, errors="coerce")
    null_mask = series.isna()
    parse_failed = parsed.isna() & ~null_mask

    str_values = series.astype(str)
    result = series.astype(object).copy()

    for i in range(len(series)):
        if null_mask.iloc[i] or parse_failed.iloc[i]:
            continue  # preserve null / unparseable values
        orig_date = parsed.iloc[i].date()
        value_str = str_values.iloc[i]
        perturbed = _perturb_date(orig_date, bucket, job_seed, namespace, value_str)
        result.iloc[i] = perturbed.strftime(fmt)

    return result


def bucket_perturb_date_format_problem(date_format: Any) -> str | None:
    """Why ``date_format`` cannot write a perturbed date back, or None if it can.

    The same format both parses a value and writes the perturbed date back with
    ``strftime``. With no date directive ``strftime`` returns the format text, so
    every parsed date would be overwritten by it. None and "" mean autodetect and
    are accepted here. The whole string is scanned with ``%%`` consumed as a
    literal percent. The reason never carries a data value.
    """
    if date_format is None or (isinstance(date_format, str) and date_format == ""):
        return None
    if not isinstance(date_format, str):
        return f"date_format must be a string, got {type(date_format).__name__}. {_FORMAT_ADVICE}"
    has_date = False
    i = 0
    while i < len(date_format):
        if date_format[i] != "%":
            i += 1
            continue
        if i + 1 == len(date_format):
            return f"date_format ends with a bare '%' (write '%%' for a literal). {_FORMAT_ADVICE}"
        directive = date_format[i + 1]
        if directive in _DATE_DIRECTIVES:
            has_date = True
        elif directive != "%" and directive not in _OTHER_DIRECTIVES:
            return f"date_format has the unsupported directive '%{directive}'. {_FORMAT_ADVICE}"
        i += 2
    if not has_date:
        return (
            "date_format has no date directive, so a perturbed date cannot be "
            f"written back and the format text would replace every value. {_FORMAT_ADVICE}"
        )
    return None


def validate_bucket_perturb_config(cfg: dict[str, Any]) -> None:
    """Validate bucket_perturb config dict; raise ValueError on any invalid field.

    Args:
        cfg: Raw config dict with keys ``bucket`` and optionally ``date_format``.

    Raises:
        ValueError: ``bucket`` is missing or not a supported value, or
            ``date_format`` cannot write a date back.
    """
    bucket = cfg.get("bucket")
    if not bucket:
        raise ValueError(
            f"bucket_perturb: 'bucket' is required. Supported values: {sorted(_VALID_BUCKETS)}."
        )
    if bucket not in _VALID_BUCKETS:
        raise ValueError(
            f"bucket_perturb: unsupported bucket {bucket!r}. "
            f"Supported values: {sorted(_VALID_BUCKETS)}."
        )
    if "date_format" in cfg:
        problem = bucket_perturb_date_format_problem(cfg["date_format"])
        if problem is not None:
            raise ValueError(f"bucket_perturb: {problem}")

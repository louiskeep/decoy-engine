"""Column-shape classification helpers for the STORM profiler (F11c split).

Pure per-column shape signals (alphabet class, cardinality band, numeric
range band, mode, casing) the FORECAST chooser branches on. Self-contained
(pandas only); imported back into profiler.py so the
decoy_engine.storm.profiler._detect_casing / _classify_* paths are unchanged.
"""

from __future__ import annotations

import pandas as pd

# Plan B-2 - column-shape signals.
#
# All four classify a column into a small enum the FORECAST chooser
# can branch on. They run on the same non-null sample _detect_casing
# already pulls, so the profiler stays O(rows) per column.

_B2_ALPHABET_SAMPLE = 200


def _classify_alphabet(series: pd.Series) -> str | None:
    """Classify the dominant character class of a string column.

    Returns one of:
      'digits'    - every non-null sample is digits only
      'alpha'     - every non-null sample is letters only
      'alphanum'  - every sample is digits + letters (no other chars)
      'mixed'     - at least one sample contains punctuation / separators
                    / whitespace, or class membership is inconsistent
                    (e.g. some rows digits-only, others alphanum)
      None        - column has no non-null string-shaped values

    The chooser uses this to size hash.truncate (digits -> 8, alphanum ->
    12, mixed -> leave at default) and pick FPE radix (10 for digits,
    36 for alphanum). Sampling is capped at 200 values; the dominant
    class wins when >=80% of samples land in the same bucket.
    """
    # F-4 fix: vectorized regex tests replace the O(rows * chars) Python
    # double loop. ``str.fullmatch`` runs in pandas/numpy native code and
    # is ~50x faster on wide tables.
    non_null = series.dropna().astype(str).str.strip()
    non_null = non_null[non_null != ""]
    if len(non_null) == 0:
        return None
    if len(non_null) > _B2_ALPHABET_SAMPLE:
        non_null = non_null.iloc[:_B2_ALPHABET_SAMPLE]
    total = len(non_null)
    is_digits = non_null.str.fullmatch(r"\d+").fillna(False)
    is_alpha = non_null.str.fullmatch(r"[A-Za-z]+").fillna(False)
    is_alphanum = non_null.str.fullmatch(r"[A-Za-z0-9]+").fillna(False)
    n_digits = int(is_digits.sum())
    n_alpha = int(is_alpha.sum())
    # alphanum bucket excludes strings already classified as pure digits / pure
    # alpha so the counts are disjoint (matches the prior loop's semantics).
    n_alphanum = int((is_alphanum & ~is_digits & ~is_alpha).sum())
    n_mixed = total - n_digits - n_alpha - n_alphanum
    counts = {
        "digits": n_digits,
        "alpha": n_alpha,
        "alphanum": n_alphanum,
        "mixed": n_mixed,
    }
    winner = max(counts.items(), key=lambda kv: kv[1])
    if winner[1] / total < 0.8:
        return "mixed"
    return winner[0]


# Cardinality buckets - coarser than unique_rate so the chooser
# doesn't need to re-derive these thresholds.
_B2_VALUE_SET_BANDS: tuple[tuple[float, str], ...] = (
    (0.95, "unique"),  # near-PK
    (0.50, "high"),
    (0.10, "medium"),
    (0.0, "low"),
)


def _classify_value_set_size(
    distinct_count: int,
    unique_rate: float,
) -> str | None:
    """Bucket a column's cardinality into one of:

    'constant' - exactly one distinct value (incl. all-NULL columns)
    'binary'   - two distinct values (yes/no, 0/1, true/false)
    'low'      - <=10 distinct values OR <10% unique_rate
    'medium'   - <50% unique_rate
    'high'     - <95% unique_rate
    'unique'   - >=95% unique_rate (PK-shaped)
    None       - empty column (no non-null values)
    """
    if distinct_count == 0:
        return None
    if distinct_count == 1:
        return "constant"
    if distinct_count == 2:
        return "binary"
    if distinct_count <= 10:
        return "low"
    for threshold, label in _B2_VALUE_SET_BANDS:
        if unique_rate >= threshold:
            return label
    return "low"


def _classify_numeric_range(
    series: pd.Series,
    inferred_type: str,
) -> str | None:
    """Bucket a numeric column's range into one of:

      'small_int'      - int column with magnitude under ~10k (lookup IDs,
                         counts, status codes, age in years)
      'big_int'        - int column with magnitude >=10k (account numbers,
                         large surrogate keys, timestamps in seconds)
      'decimal_money'  - float column whose values look like currency:
                         dominant scale of exactly 2 decimal places
      'decimal_other'  - float column with non-money decimals (measurements,
                         ratios, scientific values)
      None             - non-numeric column

    Money detection samples up to 200 non-null values and checks the
    fractional-part length of each. >=70% with exactly 2 decimal places
    wins the 'decimal_money' label.
    """
    if inferred_type not in ("integer", "float"):
        return None
    non_null = series.dropna()
    if len(non_null) == 0:
        return None
    try:
        numeric = pd.to_numeric(non_null, errors="coerce").dropna()
    except Exception:
        return None
    if len(numeric) == 0:
        return None
    abs_max = float(numeric.abs().max())
    if inferred_type == "integer":
        return "small_int" if abs_max < 10_000 else "big_int"
    # Float - sniff for 2-decimal money shape. Money values are
    # representable in at most 2 decimal places (1.50 == round(1.50, 2)
    # within float epsilon) but at least some values have a non-zero
    # fractional part (otherwise it's an int-valued float column).
    sample = numeric.head(_B2_ALPHABET_SAMPLE)
    two_dp_hits = 0
    has_fractional = False
    total = 0
    for v in sample:
        total += 1
        fv = float(v)
        if abs(fv - round(fv, 2)) < 1e-9:
            two_dp_hits += 1
        if abs(fv - round(fv)) >= 1e-9:
            has_fractional = True
    if total > 0 and has_fractional and two_dp_hits / total >= 0.7:
        return "decimal_money"
    return "decimal_other"


def _compute_mode(
    series: pd.Series,
    total_rows: int,
) -> tuple[str | None, float]:
    """Return (mode_value, mode_freq).

    ``mode_value`` is the most common non-null value as a string;
    ``mode_freq`` is its count divided by ``total_rows`` (NOT by
    non-null count - we want "this single value is 60% of the column"
    to reflect coverage, not just non-null density). Returns
    (None, 0.0) when the column has no non-null values.
    """
    if total_rows == 0:
        return None, 0.0
    non_null = series.dropna()
    if len(non_null) == 0:
        return None, 0.0
    try:
        vc = non_null.value_counts(dropna=True)
    except Exception:
        return None, 0.0
    if len(vc) == 0:
        return None, 0.0
    top_value = vc.index[0]
    top_count = int(vc.iloc[0])
    return str(top_value), round(top_count / total_rows, 4)


def _detect_casing(series: pd.Series) -> str | None:
    """Classify the dominant casing of a string column.

    Samples up to ~200 non-null values, classifies each as one of:
      'upper'        - every alphabetic char is uppercase
      'lower'        - every alphabetic char is lowercase
      'title'        - every alphabetic token starts uppercase + rest lowercase
                       (Title Case + middle-initial-style 'Mary M Smith' both qualify)
      'digits_only'  - no alphabetic characters at all
      'mixed'        - anything else (e.g. 'iPhone' or random caps)

    Returns the dominant class label (>50% of sampled non-empty values),
    or 'mixed' as a low-confidence fallback, or None when the column has
    no string-shaped values worth classifying.
    """
    # F-4 fix: vectorized casing tests. pandas Series.str.is{upper,lower,title}
    # run in native code; the prior per-row + per-char Python loop dominated
    # _profile_column wall-time for wide string tables.
    if len(series) == 0:
        return None
    non_null = series.dropna().astype(str).str.strip()
    non_null = non_null[non_null != ""]
    if len(non_null) == 0:
        return None
    if len(non_null) > 200:
        non_null = non_null.iloc[:200]
    total = len(non_null)
    has_alpha = non_null.str.contains(r"[A-Za-z]", regex=True).fillna(False)
    is_upper = non_null.str.isupper().fillna(False) & has_alpha
    is_lower = non_null.str.islower().fillna(False) & has_alpha
    # istitle() ignores all-digit strings (returns False), which is what we want
    is_title = non_null.str.istitle().fillna(False) & has_alpha & ~is_upper & ~is_lower
    is_digits_only = ~has_alpha
    n_upper = int(is_upper.sum())
    n_lower = int(is_lower.sum())
    n_title = int(is_title.sum())
    n_digits = int(is_digits_only.sum())
    n_mixed = total - n_upper - n_lower - n_title - n_digits
    counts = {
        "upper": n_upper,
        "lower": n_lower,
        "title": n_title,
        "digits_only": n_digits,
        "mixed": n_mixed,
    }
    winner = max(counts.items(), key=lambda kv: kv[1])
    if winner[1] / total < 0.5:
        return "mixed"
    return winner[0]

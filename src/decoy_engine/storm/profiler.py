"""STORM profiler — `run_storm(df, source_label, ...) -> StormProfile`.

Pure function over a pandas DataFrame. Computes per-field statistics, runs
PII / format detectors, scans for sentinel values, and rolls everything up
into a StormProfile that FORECAST consumes.

This logic was ported down from forge-platform/api/analytics/router.py
(`_profile_column` / `_profile_df` / `_reid_risk`) so the engine can run
analysis offline and from the CLI without depending on the platform.
"""

from __future__ import annotations

import warnings

import pandas as pd

from decoy_engine.context import ExecutionContext, emit_lineage, emit_step
from decoy_engine.storm.detectors import (
    hits_custom_name_hint,
    hits_name_hint,
    run_all_detectors,
)
from decoy_engine.storm.sentinels import detect_sentinels
from decoy_engine.storm.types import (
    CustomDetectorSpec,
    DetectionSignal,
    DetectorMatch,
    Distribution,
    FieldStats,
    StormProfile,
    TopValue,
)

# ── PII scoring ───────────────────────────────────────────────────────────────

# Detectors that strongly imply PII when they fire, regardless of column name.
_PII_DETECTORS = {"email", "ssn", "us_phone", "person_name", "first_name", "last_name"}

# Detectors that imply quasi-identifier territory (helpful but not PII alone).
_QUASI_ID_DETECTORS = {"us_zip", "iso_date", "us_date", "eu_date"}


def _score_pii(detector_matches: list[DetectorMatch], unique_rate: float) -> float:
    """Heuristic 0.0 – 1.0 likelihood the column is identifying."""
    if not detector_matches:
        return 0.0
    best = detector_matches[0]
    base = 0.0
    if best.detector_id in _PII_DETECTORS:
        base = 0.6 + 0.4 * best.match_rate          # 0.6 – 1.0
    elif best.detector_id in _QUASI_ID_DETECTORS:
        base = 0.3 + 0.3 * best.match_rate          # 0.3 – 0.6
    # Boost when the column is also highly unique (more identifying in practice).
    boost = min(0.15, unique_rate * 0.15) if unique_rate > 0.5 else 0.0
    return round(min(1.0, base + boost), 3)


# ── quasi-identifier co-occurrence + k-anonymity ──────────────────────────────
#
# Plan B-1: the HIPAA trio (DOB + 5-digit ZIP + gender) is the canonical
# example of a quasi-identifier combo, but treating it as the only
# possibility misses every dataset where the high-risk combo is
# something else (e.g. medication + admission_year + diagnosis_code).
# Instead, we sweep all 2- and 3-column combos of low-cardinality
# categorical candidates and report k-anonymity from the actual data.
# The HIPAA trio still emerges naturally when those columns happen to
# have the right cardinality + joint uniqueness.


# Upper bound on candidate columns to consider. C(10,2) + C(10,3) = 165
# groupbys per scan, which is well under a second on a 100k-row sample.
# Past 10 candidates the combinatorics start to bite and the lowest-k
# combo is overwhelmingly likely to be among the top-10 by distinct_count
# anyway (a column that contributes meaningful identifiability needs
# enough cardinality to discriminate, which is what the ranking proxies).
_K_ANON_MAX_CANDIDATES = 10

# Cardinality band for quasi-id candidates. Exclude high-cardinality
# columns (likely PKs / direct identifiers — those don't need a combo
# to identify rows) and near-constant ones (don't discriminate at all).
_K_ANON_MIN_UNIQUE_RATE = 0.005
_K_ANON_MAX_UNIQUE_RATE = 0.95
_K_ANON_MAX_DISTINCT_FRACTION = 0.25   # < 25% of rows distinct = categorical-like
# Absolute floor so the fraction doesn't over-filter on tiny datasets.
# A column with <=20 distinct values is categorical-like in practice
# regardless of row count.
_K_ANON_MAX_DISTINCT_ABSOLUTE = 20

# Types we'll consider for k-anonymity. Float columns with measurement
# noise rarely participate in linkage attacks because the values are
# already too granular to share across rows; the cardinality filter
# above also catches them, but being explicit keeps the groupby fast.
# "mixed" is included because STORM infers it for short-string
# categorical columns (gender, city, state) whose values don't match
# a single narrow type pattern — these are valid quasi-id candidates.
_K_ANON_TYPES = {"integer", "string", "boolean", "date", "mixed"}


def _compute_k_anonymity(
    df: pd.DataFrame,
    fields: list[FieldStats],
) -> tuple[int | None, list[list[str]]]:
    """Compute k-anonymity from low-cardinality categorical combos.

    Returns ``(k, groups)`` where:

      - ``k`` is the minimum group size produced by any 2- or 3-column
        combo of quasi-identifier candidates. ``k == 1`` means at least
        one combo uniquely identifies at least one row (high linkage
        risk). ``k == None`` means no candidates were eligible (the
        dataset is either too small, or every column is unique /
        constant — direct-identifier territory is captured separately
        by per-field ``pii_score`` and detector hits, not here).

      - ``groups`` is every combo that ties at that minimum ``k``.
        Multiple combos can produce the same k; listing all of them
        keeps the result honest and lets FORECAST see which fields
        contribute to the risk.

    Candidate selection: columns whose ``unique_rate`` lies in
    (0.005, 0.95) and ``distinct_count`` is under 25% of ``row_count``.
    Capped at ``_K_ANON_MAX_CANDIDATES`` by descending ``distinct_count``
    so the most discriminating candidates are kept when the cap binds.
    """
    row_count = len(df)
    if row_count < 2:
        return None, []

    candidates: list[FieldStats] = []
    for f in fields:
        if f.inferred_type not in _K_ANON_TYPES:
            continue
        if f.distinct_count <= 1:
            continue
        if f.unique_rate <= _K_ANON_MIN_UNIQUE_RATE:
            continue
        if f.unique_rate >= _K_ANON_MAX_UNIQUE_RATE:
            continue
        # Column qualifies if EITHER its distinct_count is small in
        # absolute terms OR a small fraction of total rows. The
        # absolute cap keeps small fixtures + small natural
        # categoricals (gender, state, status) from being over-
        # filtered when row_count * fraction lands below 1.
        max_distinct = max(
            _K_ANON_MAX_DISTINCT_ABSOLUTE,
            int(row_count * _K_ANON_MAX_DISTINCT_FRACTION),
        )
        if f.distinct_count > max_distinct:
            continue
        candidates.append(f)

    if not candidates:
        return None, []

    # Rank by distinct_count descending — high-cardinality candidates
    # are the most identifying. Tie-break alphabetically for determinism.
    candidates.sort(key=lambda f: (-f.distinct_count, f.name))
    candidate_names = [f.name for f in candidates[:_K_ANON_MAX_CANDIDATES]]

    # Defensive: drop names not actually present in the dataframe
    # (shouldn't happen given fields come from df.columns, but the
    # groupby would raise rather than skip).
    candidate_names = [n for n in candidate_names if n in df.columns]
    if len(candidate_names) < 2:
        return None, []

    from itertools import combinations

    best_k: int | None = None
    best_groups: list[list[str]] = []
    for size in (2, 3):
        if size > len(candidate_names):
            break
        for combo in combinations(candidate_names, size):
            try:
                sub = df.loc[:, list(combo)].dropna()
                if len(sub) == 0:
                    continue
                k = int(sub.groupby(list(combo), dropna=False).size().min())
            except Exception:
                # Defensive: an unhashable column value (rare — dict /
                # list cells from JSON-typed sources) would blow up the
                # groupby. Skip silently rather than 500 the scan.
                continue
            if best_k is None or k < best_k:
                best_k = k
                best_groups = [list(combo)]
            elif k == best_k:
                best_groups.append(list(combo))

    return best_k, best_groups


# ── per-column profiler ───────────────────────────────────────────────────────

def _top_values(series: pd.Series, total_rows: int, n: int = 5) -> list[TopValue]:
    vc = series.value_counts(dropna=False).head(n)
    out: list[TopValue] = []
    for val, cnt in vc.items():
        is_na = val is None or (isinstance(val, float) and pd.isna(val))
        out.append(TopValue(
            value="(null)" if is_na else str(val),
            count=int(cnt),
            pct=round(cnt / total_rows * 100, 1) if total_rows > 0 else 0.0,
        ))
    return out


def _date_format_signal(detector_matches: list[DetectorMatch]) -> str | None:
    """If any of the date detectors fired, surface its id as a format signal."""
    for m in detector_matches:
        if m.detector_id in ("iso_date", "us_date", "eu_date"):
            return m.detector_id
    return None


def _format_pattern_from_detectors(
    detector_matches: list[DetectorMatch],
) -> str | None:
    """Pick the winning detector's ``format_pattern`` to surface on FieldStats.

    `run_all_detectors` returns matches sorted by descending match_rate, so
    the first match with a non-None format_pattern is the dominant variant
    for the column. Detectors without a format-variant table (email, name,
    etc.) emit format_pattern=None and are silently skipped.
    """
    for m in detector_matches:
        if m.format_pattern is not None:
            return m.format_pattern
    return None


# Plan B-2 — column-shape signals.
#
# All four classify a column into a small enum the FORECAST chooser
# can branch on. They run on the same non-null sample _detect_casing
# already pulls, so the profiler stays O(rows) per column.

_B2_ALPHABET_SAMPLE = 200


def _classify_alphabet(series: pd.Series) -> str | None:
    """Classify the dominant character class of a string column.

    Returns one of:
      'digits'    — every non-null sample is digits only
      'alpha'     — every non-null sample is letters only
      'alphanum'  — every sample is digits + letters (no other chars)
      'mixed'     — at least one sample contains punctuation / separators
                    / whitespace, or class membership is inconsistent
                    (e.g. some rows digits-only, others alphanum)
      None        — column has no non-null string-shaped values

    The chooser uses this to size hash.truncate (digits → 8, alphanum →
    12, mixed → leave at default) and pick FPE radix (10 for digits,
    36 for alphanum). Sampling is capped at 200 values; the dominant
    class wins when >=80% of samples land in the same bucket.
    """
    non_null = series.dropna()
    if len(non_null) == 0:
        return None
    if len(non_null) > _B2_ALPHABET_SAMPLE:
        non_null = non_null.head(_B2_ALPHABET_SAMPLE)
    counts = {"digits": 0, "alpha": 0, "alphanum": 0, "mixed": 0}
    total = 0
    for v in non_null.astype(str):
        s = v.strip()
        if not s:
            continue
        total += 1
        has_digit = False
        has_alpha = False
        has_other = False
        for c in s:
            if c.isdigit():
                has_digit = True
            elif c.isalpha():
                has_alpha = True
            else:
                has_other = True
                break
        if has_other:
            counts["mixed"] += 1
        elif has_digit and has_alpha:
            counts["alphanum"] += 1
        elif has_digit:
            counts["digits"] += 1
        elif has_alpha:
            counts["alpha"] += 1
        else:
            counts["mixed"] += 1
    if total == 0:
        return None
    winner = max(counts.items(), key=lambda kv: kv[1])
    if winner[1] / total < 0.8:
        return "mixed"
    return winner[0]


# Cardinality buckets — coarser than unique_rate so the chooser
# doesn't need to re-derive these thresholds.
_B2_VALUE_SET_BANDS: tuple[tuple[float, str], ...] = (
    (0.95, "unique"),     # near-PK
    (0.50, "high"),
    (0.10, "medium"),
    (0.0, "low"),
)


def _classify_value_set_size(
    distinct_count: int,
    unique_rate: float,
) -> str | None:
    """Bucket a column's cardinality into one of:

      'constant' — exactly one distinct value (incl. all-NULL columns)
      'binary'   — two distinct values (yes/no, 0/1, true/false)
      'low'      — <=10 distinct values OR <10% unique_rate
      'medium'   — <50% unique_rate
      'high'     — <95% unique_rate
      'unique'   — >=95% unique_rate (PK-shaped)
      None       — empty column (no non-null values)
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

      'small_int'      — int column with magnitude under ~10k (lookup IDs,
                         counts, status codes, age in years)
      'big_int'        — int column with magnitude >=10k (account numbers,
                         large surrogate keys, timestamps in seconds)
      'decimal_money'  — float column whose values look like currency:
                         dominant scale of exactly 2 decimal places
      'decimal_other'  — float column with non-money decimals (measurements,
                         ratios, scientific values)
      None             — non-numeric column

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
    # Float — sniff for 2-decimal money shape. Money values are
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
    non-null count — we want "this single value is 60% of the column"
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
      'upper'        — every alphabetic char is uppercase
      'lower'        — every alphabetic char is lowercase
      'title'        — every alphabetic token starts uppercase + rest lowercase
                       (Title Case + middle-initial-style 'Mary M Smith' both qualify)
      'digits_only'  — no alphabetic characters at all
      'mixed'        — anything else (e.g. 'iPhone' or random caps)

    Returns the dominant class label (>50% of sampled non-empty values),
    or 'mixed' as a low-confidence fallback, or None when the column has
    no string-shaped values worth classifying.
    """
    if len(series) == 0:
        return None
    non_null = series.dropna()
    if len(non_null) == 0:
        return None
    if len(non_null) > 200:
        non_null = non_null.head(200)
    counts: dict[str, int] = {}
    total = 0
    for v in non_null.astype(str):
        s = v.strip()
        if not s:
            continue
        total += 1
        # digits_only first — most columns of pure-numeric strings are
        # IDs, ZIPs, phones, etc. where preserving "no casing" matters.
        if not any(c.isalpha() for c in s):
            label = "digits_only"
        elif s.isupper():
            label = "upper"
        elif s.islower():
            label = "lower"
        elif s.istitle():
            label = "title"
        else:
            label = "mixed"
        counts[label] = counts.get(label, 0) + 1
    if total == 0:
        return None
    winner = max(counts.items(), key=lambda kv: kv[1])
    if winner[1] / total < 0.5:
        return "mixed"
    return winner[0]


# ── distribution + detection trail ────────────────────────────────────────────

# Cardinality threshold for "categorical" object columns. Below this we group
# by value and show top-10 + other; above it we treat the column as freetext
# or pattern-shaped depending on whether a detector fired.
_CATEGORICAL_DISTINCT_CAP = 30


def _distribution_numeric(non_null: pd.Series) -> Distribution | None:
    """10 equal-width bins between min and max, with counts + min/max/mean."""
    series = pd.to_numeric(non_null, errors="coerce").dropna()
    if len(series) == 0:
        return None
    # When all values are identical pd.cut errors on zero range — fall back to
    # a single-bin histogram so the renderer still has something to show.
    if series.min() == series.max():
        return Distribution(
            kind="numeric",
            data=[float(len(series))],
            labels=[str(series.iloc[0])],
            min=str(series.min()),
            max=str(series.max()),
            mean=round(float(series.mean()), 4),
        )
    binned = pd.cut(series, bins=10, include_lowest=True)
    bin_counts = binned.value_counts().sort_index()
    data = [int(c) for c in bin_counts.values]
    # Label format: prefer integers when bin width spans more than 1 unit;
    # otherwise show two decimals so tight ranges stay readable.
    labels = []
    for iv in bin_counts.index:
        if (iv.right - iv.left) >= 1:
            labels.append(f"{int(iv.left)}-{int(iv.right)}")
        else:
            labels.append(f"{iv.left:.2f}-{iv.right:.2f}")
    return Distribution(
        kind="numeric",
        data=[float(d) for d in data],
        labels=labels,
        min=str(series.min()),
        max=str(series.max()),
        mean=round(float(series.mean()), 4),
    )


def _distribution_date(non_null: pd.Series) -> Distribution | None:
    """Decade bins on parsed date values."""
    coerced = pd.to_datetime(non_null, errors="coerce").dropna()
    if len(coerced) == 0:
        return None
    decades = (coerced.dt.year // 10) * 10
    by_decade = decades.value_counts().sort_index()
    return Distribution(
        kind="date",
        data=[float(c) for c in by_decade.values],
        labels=[f"{int(d)}s" for d in by_decade.index],
        min=coerced.min().date().isoformat(),
        max=coerced.max().date().isoformat(),
    )


def _distribution_categorical(non_null: pd.Series, total_rows: int) -> Distribution | None:
    """Top-10 + 'other' as percent of column."""
    if total_rows == 0 or len(non_null) == 0:
        return None
    vc = non_null.value_counts()
    top = vc.head(10)
    other = int(vc.iloc[10:].sum()) if len(vc) > 10 else 0
    data = [round(float(c) / total_rows * 100, 1) for c in top.values]
    labels = [str(v) for v in top.index]
    if other > 0:
        data.append(round(other / total_rows * 100, 1))
        labels.append("other")
    return Distribution(kind="categorical", data=data, labels=labels)


def _distribution_pattern(non_null: pd.Series, winning: DetectorMatch) -> Distribution:
    """3 buckets for detector-fired columns: matches / no-match / null is 0
    here because we operate on non_null. Reported as pct of non-null."""
    n = len(non_null)
    matches = round(winning.match_rate * 100, 1)
    miss = round(100.0 - matches, 1) if matches < 100.0 else 0.0
    return Distribution(
        kind="pattern",
        data=[matches, miss],
        labels=[f"matches {winning.detector_id}", "doesn't match"],
        min=None,
        max=str(n),
    )


def _distribution_freetext(non_null: pd.Series) -> Distribution | None:
    """Length buckets <20 / 20-50 / 50-100 / >100 chars."""
    if len(non_null) == 0:
        return None
    lens = non_null.astype(str).str.len()
    buckets = [
        ("<20",     int((lens < 20).sum())),
        ("20-50",   int(((lens >= 20) & (lens < 50)).sum())),
        ("50-100",  int(((lens >= 50) & (lens < 100)).sum())),
        (">100",    int((lens >= 100).sum())),
    ]
    return Distribution(
        kind="freetext",
        data=[float(c) for c in [b[1] for b in buckets]],
        labels=[b[0] for b in buckets],
        min=str(int(lens.min())),
        max=str(int(lens.max())),
        mean=round(float(lens.mean()), 1),
    )


def _build_distribution(
    series: pd.Series,
    inferred_type: str,
    detector_matches: list[DetectorMatch],
    distinct_count: int,
    total_rows: int,
) -> Distribution | None:
    """Pick a distribution shape based on dtype + cardinality + detector hits."""
    non_null = series.dropna()
    if len(non_null) == 0:
        return None

    if inferred_type in ("integer", "float"):
        return _distribution_numeric(non_null)

    # Native datetime columns map directly to date bins.
    if inferred_type == "date":
        return _distribution_date(non_null)

    if inferred_type == "string":
        # Categorical when low cardinality regardless of detector — value-set is
        # the most useful summary at that scale.
        if distinct_count > 0 and distinct_count <= _CATEGORICAL_DISTINCT_CAP:
            return _distribution_categorical(non_null, total_rows)
        # If a detector fired, surface match-rate buckets; the user wants to
        # see "97.8% of values look like SSN" at a glance.
        if detector_matches:
            return _distribution_pattern(non_null, detector_matches[0])
        # Otherwise the column is high-cardinality free-form text.
        return _distribution_freetext(non_null)

    return None


def _build_detection_trail(
    detector_matches: list[DetectorMatch],
    col_name: str,
    custom: list[CustomDetectorSpec] | None = None,
) -> list[DetectionSignal]:
    """Emit reasoning rows for the winning detector.

    Today: the regex score (always) + name-hint row (when the column name
    matched the detector's name pattern). For custom detectors the trail
    consults the provided spec list since name-hint resolution there is
    spec-driven rather than module-global. ML rows append in Roadmap Item 8.
    """
    if not detector_matches:
        return []
    winner = detector_matches[0]
    rows: list[DetectionSignal] = [
        DetectionSignal(
            signal=f"regex · {winner.detector_id}_pattern",
            confidence=round(winner.match_rate * 100, 2),
            winner=True,
            ml=False,
        ),
    ]
    # Name-hint lookup: built-in detectors use the module dict; custom ones
    # use the spec list the caller passed in. The id namespace prevents
    # ambiguity when both have the same id (custom wins because we check
    # specs first — but the registry layer already guarantees no collision).
    name_hint_matched = False
    if custom:
        spec = next((s for s in custom if s.id == winner.detector_id), None)
        if spec is not None:
            name_hint_matched = hits_custom_name_hint(spec, col_name)
    if not name_hint_matched and hits_name_hint(winner.detector_id, col_name):
        name_hint_matched = True
    if name_hint_matched:
        rows.append(DetectionSignal(
            signal=f'name-hint · col="{col_name}"',
            confidence=100.0,
            winner=False,
            ml=False,
        ))
    return rows


def _profile_column(
    series: pd.Series,
    total_rows: int,
    custom_detectors: list[CustomDetectorSpec] | None = None,
) -> FieldStats:
    name = str(series.name)
    dtype_raw = str(series.dtype)
    null_count = int(series.isna().sum())
    null_rate = round(null_count / total_rows, 4) if total_rows > 0 else 0.0
    non_null = series.dropna()
    non_null_count = len(non_null)
    distinct_count = int(series.nunique(dropna=True))
    unique_rate = round(distinct_count / non_null_count, 4) if non_null_count > 0 else 0.0
    is_likely_unique = unique_rate > 0.9 and distinct_count > 1

    # Inferred type — a friendlier label than the raw pandas dtype.
    if pd.api.types.is_datetime64_any_dtype(series):
        inferred = "date"
    elif pd.api.types.is_bool_dtype(series):
        inferred = "boolean"
    elif pd.api.types.is_integer_dtype(series):
        inferred = "integer"
    elif pd.api.types.is_float_dtype(series):
        inferred = "float"
    elif pd.api.types.is_string_dtype(series):
        inferred = "string"
    else:
        inferred = "mixed"

    fs = FieldStats(
        name=name,
        inferred_type=inferred,
        dtype_raw=dtype_raw,
        row_count=total_rows,
        null_count=null_count,
        null_rate=null_rate,
        distinct_count=distinct_count,
        unique_rate=unique_rate,
        is_likely_unique=is_likely_unique,
        top_values=_top_values(series, total_rows),
    )

    # Numeric stats.
    if pd.api.types.is_numeric_dtype(series) and non_null_count > 0:
        fs.min_value = str(non_null.min())
        fs.max_value = str(non_null.max())
        fs.mean_value = str(round(float(non_null.mean()), 4))

    # Native datetime stats.
    if pd.api.types.is_datetime64_any_dtype(series) and non_null_count > 0:
        fs.date_min = non_null.min().isoformat()
        fs.date_max = non_null.max().isoformat()

    # String length + date sniffing on string-typed columns (object or native str dtype).
    if pd.api.types.is_string_dtype(series) and non_null_count > 0:
        str_lens = non_null.astype(str).str.len()
        fs.min_length = int(str_lens.min())
        fs.max_length = int(str_lens.max())
        fs.avg_length = round(float(str_lens.mean()), 1)

        sample_size = min(200, non_null_count)
        # Format inference is the whole point here — pandas 2.x emits a chatty
        # UserWarning when it falls back to dateutil. Suppress it; we are
        # deliberately probing for a date-shaped column without specifying
        # a format up front.
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=".*Could not infer format.*",
                category=UserWarning,
            )
            coerced_sample = pd.to_datetime(non_null.head(sample_size), errors="coerce")
            parse_rate = coerced_sample.notna().sum() / sample_size if sample_size else 0
            if parse_rate >= 0.7:
                all_coerced = pd.to_datetime(non_null, errors="coerce")
                valid = all_coerced.dropna()
                invalid_mask = all_coerced.isna()
                if len(valid) > 0:
                    fs.date_min = valid.min().isoformat()
                    fs.date_max = valid.max().isoformat()
                fs.invalid_count = int(invalid_mask.sum())
                if fs.invalid_count > 0:
                    fs.sample_invalid = non_null[invalid_mask].head(3).astype(str).tolist()

    # Detectors.
    fs.detector_matches = run_all_detectors(series, name, custom=custom_detectors)
    fs.date_format = _date_format_signal(fs.detector_matches)
    # Item 65 — format-preservation hints. Both fields are optional; the
    # masking-strategy post-pass reads them when `preserve_format=true`.
    fs.format_pattern = _format_pattern_from_detectors(fs.detector_matches)
    fs.casing_pattern = _detect_casing(series)

    # Plan B-2 — column-shape signals FORECAST choosers read.
    if pd.api.types.is_string_dtype(series) and non_null_count > 0:
        fs.alphabet = _classify_alphabet(series)
    fs.value_set_size_class = _classify_value_set_size(distinct_count, unique_rate)
    fs.numeric_range_class = _classify_numeric_range(series, inferred)
    fs.mode_value, fs.mode_freq = _compute_mode(series, total_rows)

    # Sentinels.
    fs.sentinels = detect_sentinels(series, name)

    # PII score.
    fs.pii_score = _score_pii(fs.detector_matches, fs.unique_rate)

    # Distribution + detection trail (PR3). Distribution shape depends on
    # dtype + cardinality + whether a detector fired; trail records the
    # reasoning behind the winning detector. Both are JSON-serializable
    # via dataclasses.asdict and degrade to None / [] when nothing fires.
    fs.distribution = _build_distribution(
        series, inferred, fs.detector_matches, distinct_count, total_rows,
    )
    fs.detection_trail = _build_detection_trail(fs.detector_matches, name, custom_detectors)

    return fs


# ── public entry point ───────────────────────────────────────────────────────

def run_storm(
    df: pd.DataFrame,
    source_label: str,
    *,
    sample_strategy: str = "full",
    sample_row_cap: int | None = None,
    custom_detectors: list[CustomDetectorSpec] | None = None,
    ctx: ExecutionContext | None = None,
) -> StormProfile:
    """Scan a DataFrame and produce a StormProfile.

    The DataFrame is the only place raw data lives in this call. The returned
    StormProfile is JSON-serializable and the ONLY thing FORECAST sees.

    `custom_detectors` (PR6) lets callers register organization-specific PII
    patterns at scan time without touching the engine. Each spec runs against
    every column alongside the built-ins; their hits show up in
    DetectorMatch.detector_id with whatever id the caller supplied.

    ``ctx`` (Item 71) routes structured events through the caller's
    JobLogger so a standalone STORM scan shows up in the bottom-pane
    SSE stream + step timeline + lineage strip the same way a masking
    job does. None preserves the pure-function behavior for CLI / test
    callers that don't have a Job bound.
    """
    logger = ctx.logger if ctx is not None else None
    emit_lineage(logger, "source", source_label, "dataset")
    emit_step(logger, "storm.scan", status="start")
    # Narrative log lines so the Job Detail page tells the
    # operator what STORM is actually doing. Before this, the
    # log carried just "▶ storm.scan" / "✓ storm.scan" -- the
    # 30-second gap between them was a black box.
    custom_count = len(custom_detectors) if custom_detectors else 0
    if logger is not None:
        logger.info(
            f"▶ profiling {len(df.columns)} columns x {len(df):,} rows "
            f"(sample_strategy={sample_strategy}"
            + (f", cap={sample_row_cap:,}" if sample_row_cap else "")
            + (f", custom_detectors={custom_count}" if custom_count else "")
            + ")"
        )
    try:
        total = len(df)
        fields: list = []
        # Tally detector hits as columns are profiled so we can emit
        # a one-line summary of which detectors fired -- saves the
        # operator from manually counting in the field table.
        detector_hits: dict[str, int] = {}
        pii_count = 0
        for col in df.columns:
            f = _profile_column(df[col], total, custom_detectors)
            fields.append(f)
            for dm in (f.detector_matches or []):
                detector_hits[dm.detector_id] = detector_hits.get(dm.detector_id, 0) + 1
            if (getattr(f, "pii_score", 0.0) or 0.0) > 0:
                pii_count += 1

        if logger is not None:
            if detector_hits:
                top = sorted(detector_hits.items(), key=lambda kv: -kv[1])
                summary = ", ".join(f"{name} x{n}" for name, n in top[:8])
                more = "" if len(top) <= 8 else f" (+{len(top) - 8} more)"
                logger.info(
                    f"✓ detector pass: {len(fields)} fields scanned, "
                    f"{pii_count} flagged PII; hits: {summary}{more}"
                )
            else:
                logger.info(
                    f"✓ detector pass: {len(fields)} fields scanned, "
                    f"no detector hits"
                )
            logger.info("▶ k-anonymity / re-id risk computation")

        # Plan B-1: data-driven k-anonymity replaces the old
        # "% of unique columns" heuristic. k_anonymity is the
        # minimum joint group size across 2- and 3-column combos
        # of quasi-id candidates; reid_risk_score is now 100/k
        # capped at 100, with 0 meaning "no quasi-id linkage
        # risk" (direct identifiers are surfaced via per-field
        # pii_score, not duplicated here).
        k_anonymity, qi_groups = _compute_k_anonymity(df, fields)
        if k_anonymity is not None and k_anonymity > 0:
            reid_score = round(min(100.0, 100.0 / k_anonymity), 1)
        else:
            reid_score = 0.0
        # The flat union of column names from the winning combos —
        # what UI consumers want to highlight as the contributing
        # columns. Falls back to an empty list when no QI combos
        # exist.
        reid_cols = sorted({col for group in qi_groups for col in group})

        if logger is not None:
            if k_anonymity is not None:
                logger.info(
                    f"✓ k-anonymity = {k_anonymity}, re-id risk = {reid_score:.1f}%; "
                    f"{len(qi_groups)} quasi-identifier combination"
                    + ("" if len(qi_groups) == 1 else "s")
                )
            else:
                logger.info("✓ no quasi-identifier combinations found (re-id risk = 0)")

        profile = StormProfile(
            source_label=source_label,
            row_count=total,
            sample_strategy=sample_strategy,
            sample_row_cap=sample_row_cap,
            fields=fields,
            reid_risk_columns=reid_cols,
            reid_risk_score=reid_score,
            quasi_identifier_groups=qi_groups,
            k_anonymity=k_anonymity,
        )
    except Exception as exc:
        if logger is not None:
            logger.error(f"✗ storm.scan failed: {type(exc).__name__}: {exc}")
        emit_step(
            logger, "storm.scan", status="error",
            error_class=type(exc).__name__, error_msg=str(exc),
        )
        raise
    if logger is not None:
        logger.info(
            f"✓ storm.scan complete: {len(fields)} fields "
            f"({pii_count} PII, re-id risk {reid_score:.1f}%)"
        )
    emit_step(
        logger, "storm.scan", status="finish",
        rows_in=total, rows_out=len(fields),
    )
    return profile

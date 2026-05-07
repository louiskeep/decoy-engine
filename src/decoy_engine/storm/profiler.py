"""STORM profiler — `run_storm(df, source_label, ...) -> StormProfile`.

Pure function over a pandas DataFrame. Computes per-field statistics, runs
PII / format detectors, scans for sentinel values, and rolls everything up
into a StormProfile that FORECAST consumes.

This logic was ported down from forge-platform/api/analytics/router.py
(`_profile_column` / `_profile_df` / `_reid_risk`) so the engine can run
analysis offline and from the CLI without depending on the platform.
"""

from __future__ import annotations

import re
import warnings
from typing import Optional

import pandas as pd

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
    SentinelFlag,
    StormProfile,
    TopValue,
)


# ── PII scoring ───────────────────────────────────────────────────────────────

# Detectors that strongly imply PII when they fire, regardless of column name.
_PII_DETECTORS = {"email", "ssn", "us_phone", "person_name"}

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


# ── quasi-identifier co-occurrence ────────────────────────────────────────────

# Classic HIPAA-style trio. Re-identification literature shows ~87% of US
# residents are unique on (DOB, 5-digit ZIP, gender). FORECAST flags this.
_DOB_HINTS    = re.compile(r"(?i)^(.*[_-])?(dob|birth.?date|date.?of.?birth)([_-].*)?$")
_ZIP_HINTS    = re.compile(r"(?i)^(.*[_-])?(zip|postal|post)([_-]?code)?([_-].*)?$")
_GENDER_HINTS = re.compile(r"(?i)^(.*[_-])?(gender|sex)([_-].*)?$")


def _quasi_identifier_groups(fields: list[FieldStats]) -> list[list[str]]:
    """Detect known re-identification quasi-identifier groups by column name."""
    groups: list[list[str]] = []
    dob    = next((f.name for f in fields if _DOB_HINTS.fullmatch(f.name or "")), None)
    zip_   = next((f.name for f in fields if _ZIP_HINTS.fullmatch(f.name or "")), None)
    gender = next((f.name for f in fields if _GENDER_HINTS.fullmatch(f.name or "")), None)
    if dob and zip_ and gender:
        groups.append([dob, zip_, gender])
    return groups


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


def _date_format_signal(detector_matches: list[DetectorMatch]) -> Optional[str]:
    """If any of the date detectors fired, surface its id as a format signal."""
    for m in detector_matches:
        if m.detector_id in ("iso_date", "us_date", "eu_date"):
            return m.detector_id
    return None


# ── distribution + detection trail ────────────────────────────────────────────

# Cardinality threshold for "categorical" object columns. Below this we group
# by value and show top-10 + other; above it we treat the column as freetext
# or pattern-shaped depending on whether a detector fired.
_CATEGORICAL_DISTINCT_CAP = 30


def _distribution_numeric(non_null: pd.Series) -> Optional[Distribution]:
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
            labels.append(f"{int(iv.left)}–{int(iv.right)}")
        else:
            labels.append(f"{iv.left:.2f}–{iv.right:.2f}")
    return Distribution(
        kind="numeric",
        data=[float(d) for d in data],
        labels=labels,
        min=str(series.min()),
        max=str(series.max()),
        mean=round(float(series.mean()), 4),
    )


def _distribution_date(non_null: pd.Series) -> Optional[Distribution]:
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


def _distribution_categorical(non_null: pd.Series, total_rows: int) -> Optional[Distribution]:
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


def _distribution_freetext(non_null: pd.Series) -> Optional[Distribution]:
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
) -> Optional[Distribution]:
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
    custom: Optional[list[CustomDetectorSpec]] = None,
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
    custom_detectors: Optional[list[CustomDetectorSpec]] = None,
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
    elif dtype_raw == "object":
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

    # String length + date sniffing on object columns.
    if dtype_raw == "object" and non_null_count > 0:
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
    sample_row_cap: Optional[int] = None,
    custom_detectors: Optional[list[CustomDetectorSpec]] = None,
) -> StormProfile:
    """Scan a DataFrame and produce a StormProfile.

    The DataFrame is the only place raw data lives in this call. The returned
    StormProfile is JSON-serializable and the ONLY thing FORECAST sees.

    `custom_detectors` (PR6) lets callers register organization-specific PII
    patterns at scan time without touching the engine. Each spec runs against
    every column alongside the built-ins; their hits show up in
    DetectorMatch.detector_id with whatever id the caller supplied.
    """
    total = len(df)
    fields = [_profile_column(df[col], total, custom_detectors) for col in df.columns]
    reid_cols = [f.name for f in fields if f.is_likely_unique]
    reid_score = round(len(reid_cols) / max(len(fields), 1) * 100, 1)
    qi_groups = _quasi_identifier_groups(fields)

    return StormProfile(
        source_label=source_label,
        row_count=total,
        sample_strategy=sample_strategy,
        sample_row_cap=sample_row_cap,
        fields=fields,
        reid_risk_columns=reid_cols,
        reid_risk_score=reid_score,
        quasi_identifier_groups=qi_groups,
    )

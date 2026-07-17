"""Per-column distribution builders for the STORM profiler (F11c split).

Build the small Distribution summaries (numeric bins, decade bins, top-k
categorical, detector match-rate, freetext length buckets) shown per column.
Self-contained; imported back into profiler.py via _build_distribution.
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.storm.types import DetectorMatch, Distribution

# Cardinality threshold for "categorical" object columns. Below this we group
# by value and show top-10 + other; above it we treat the column as freetext
# or pattern-shaped depending on whether a detector fired. Deliberately
# independent of quality/snapshot.py's fit-time _CATEGORICAL_DISTINCT_CAP
# (HC-5 D4): this is a profiler-chart display concern, not the fit source of
# truth, so it is not imported from there even though the value matches.
_STORM_CATEGORICAL_DISTINCT_CAP = 30


def _distribution_numeric(non_null: pd.Series) -> Distribution | None:
    """10 equal-width bins between min and max, with counts + min/max/mean."""
    series = pd.to_numeric(non_null, errors="coerce").dropna()
    if len(series) == 0:
        return None
    # When all values are identical pd.cut errors on zero range - fall back to
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
        ("<20", int((lens < 20).sum())),
        ("20-50", int(((lens >= 20) & (lens < 50)).sum())),
        ("50-100", int(((lens >= 50) & (lens < 100)).sum())),
        (">100", int((lens >= 100).sum())),
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
        # Categorical when low cardinality regardless of detector - value-set is
        # the most useful summary at that scale.
        if distinct_count > 0 and distinct_count <= _STORM_CATEGORICAL_DISTINCT_CAP:
            return _distribution_categorical(non_null, total_rows)
        # If a detector fired, surface match-rate buckets; the user wants to
        # see "97.8% of values look like SSN" at a glance.
        if detector_matches:
            return _distribution_pattern(non_null, detector_matches[0])
        # Otherwise the column is high-cardinality free-form text.
        return _distribution_freetext(non_null)

    return None

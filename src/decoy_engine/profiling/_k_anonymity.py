"""Quasi-identifier k-anonymity, split from profiler.py.

Each candidate column is factorized once; every 2- and 3-column combo then
reduces to integer arithmetic on the codes. The old per-combo ``groupby``
re-factorized the same columns up to 165 times, which dominated a full-frame
profile. Output matches the old groupby except for two cases it got wrong or
skipped: a category-dtype column with unused categories (the old
``observed=False`` groupby counted empty groups, giving k=0, now k=1) and
mixed-object columns the old groupby skipped (now reported).
"""

from __future__ import annotations

from itertools import combinations

import numpy as np
import pandas as pd

from decoy_engine.profiling.types import FieldStats

# ── quasi-identifier co-occurrence + k-anonymity ──
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
# combos per scan.
# Past 10 candidates the combinatorics start to bite and the lowest-k
# combo is overwhelmingly likely to be among the top-10 by distinct_count
# anyway (a column that contributes meaningful identifiability needs
# enough cardinality to discriminate, which is what the ranking proxies).
_K_ANON_MAX_CANDIDATES = 10

# Cardinality band for quasi-id candidates. Exclude high-cardinality
# columns (likely PKs / direct identifiers - those don't need a combo
# to identify rows) and near-constant ones (don't discriminate at all).
_K_ANON_MIN_UNIQUE_RATE = 0.005
_K_ANON_MAX_UNIQUE_RATE = 0.95
_K_ANON_MAX_DISTINCT_FRACTION = 0.25  # < 25% of rows distinct = categorical-like
# Absolute floor so the fraction doesn't over-filter on tiny datasets.
# A column with <=20 distinct values is categorical-like in practice
# regardless of row count.
_K_ANON_MAX_DISTINCT_ABSOLUTE = 20

# Types we'll consider for k-anonymity. Float columns with measurement
# noise rarely participate in linkage attacks because the values are
# already too granular to share across rows; the cardinality filter
# above also catches them, but being explicit keeps the groupby fast.
# "mixed" is included because Pre-scan infers it for short-string
# categorical columns (gender, city, state) whose values don't match
# a single narrow type pattern - these are valid quasi-id candidates.
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
        constant - direct-identifier territory is captured separately
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

    # Rank by distinct_count descending - high-cardinality candidates
    # are the most identifying. Tie-break alphabetically for determinism.
    candidates.sort(key=lambda f: (-f.distinct_count, f.name))
    candidate_names = [f.name for f in candidates[:_K_ANON_MAX_CANDIDATES]]

    # Defensive: drop names not actually present in the dataframe
    # (shouldn't happen given fields come from df.columns, but the
    # groupby would raise rather than skip).
    candidate_names = [n for n in candidate_names if n in df.columns]
    if len(candidate_names) < 2:
        return None, []

    codes: dict[str, tuple[np.ndarray, int]] = {}
    for name in candidate_names:
        try:
            c, uniques = pd.factorize(df[name])
        except (TypeError, ValueError):
            # Unhashable cell values (dict / list from JSON-typed sources)
            # raise TypeError; skip every combo that needs the column.
            # MemoryError + other exceptions propagate.
            continue
        codes[name] = (c, len(uniques))

    best_k: int | None = None
    best_groups: list[list[str]] = []
    for size in (2, 3):
        if size > len(candidate_names):
            break
        for combo in combinations(candidate_names, size):
            if any(n not in codes for n in combo):
                continue
            k = _min_group_size([codes[n] for n in combo])
            if k is None:
                continue
            if best_k is None or k < best_k:
                best_k = k
                best_groups = [list(combo)]
            elif k == best_k:
                best_groups.append(list(combo))

    return best_k, best_groups


_INT64_SAFE = 1 << 62


def _min_group_size(cols: list[tuple[np.ndarray, int]]) -> int | None:
    """Smallest group size over rows with no null in any column, None when no row qualifies."""
    keep = cols[0][0] >= 0
    for c, _ in cols[1:]:
        keep &= c >= 0
    if not keep.any():
        return None
    key = cols[0][0][keep].astype(np.int64)
    card = cols[0][1]
    for c, n in cols[1:]:
        if card * n >= _INT64_SAFE:
            # Compact the running key so the mixed-radix product cannot overflow.
            key, inverse = np.unique(key, return_inverse=True)
            key, card = inverse.astype(np.int64), len(key)
        key = key * n + c[keep]
        card *= n
    return int(np.unique(key, return_counts=True)[1].min())

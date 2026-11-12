"""Unique-value scoring core for the regex detectors, split from detectors.py (H-1 E1.1).

A column is stringified once and factorized: each distinct value is matched
against a pattern once, and the result is weighted by how often the value
occurs. The decision (match rate, sample misses, dominant format variant) is
the same as matching every cell, because a pattern and a validator are pure
functions of the value. Nothing here samples rows, so a late or rare value
still counts.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import NamedTuple

import numpy as np
import pandas as pd

from decoy_engine.detection.domains import domain_for
from decoy_engine.detection.types import DetectorMatch

# ── thresholds ──
#
# Detection sprint (V1) lowered the no-hint firing floor from 0.7 to 0.5 so
# medium-confidence finds reach the 3-bucket UI ("needs review") instead of
# being silently dropped. Per-bucket bounds:
#   high   = (name_hint AND rate >= 0.45) OR (rate >= 0.75)
#   medium = no_hint AND rate in [0.50, 0.75), or name_hint AND rate in [0.40, 0.45)
#   low    = no_hint AND rate in [0.30, 0.50)   (opt-in; not fired by default)
#
# The low floor is exposed as a constant so the platform can lower the
# firing threshold behind a "weak signals" flag in V1.5 without re-deriving
# the bucket math.

DEFAULT_MIN_MATCH_RATE = 0.5  # 50% of non-null values must match (no hint)
NAME_HINT_MIN_RATE = 0.4  # 40% if the column name strongly hints
LOW_CONFIDENCE_NO_HINT_FLOOR = 0.3  # opt-in floor for surfacing low-confidence finds
HIGH_CONFIDENCE_NO_HINT_FLOOR = 0.75  # content alone, name hint absent
HIGH_CONFIDENCE_WITH_HINT_FLOOR = 0.45  # content alongside a confirming name hint
SAMPLE_MISS_LIMIT = 3


class PreparedValues:
    """Non-null, stringified, stripped column values held as uniques + counts."""

    __slots__ = ("codes", "counts", "n", "raw_uniques", "uniques")

    def __init__(self, series: pd.Series) -> None:
        strs = series.dropna().astype(str)
        codes, uniques = pd.factorize(strs, sort=False)
        self.n: int = len(strs)
        self.codes: np.ndarray = codes
        self.raw_uniques: pd.Series = pd.Series(uniques.astype(object))
        # Stripping the distinct values is the same as stripping every cell.
        self.uniques: pd.Series = self.raw_uniques.str.strip()
        self.counts: np.ndarray = np.bincount(codes, minlength=len(uniques))

    def length_stats(self) -> tuple[int, int, float]:
        """(min, max, mean) of the unstripped string length over every cell."""
        lens = self.raw_uniques.str.len().to_numpy(dtype=np.int64)
        return int(lens.min()), int(lens.max()), float((lens * self.counts).sum()) / self.n


# One column at a time: run_all_detectors opens a scope so the ~25 detectors
# share a single stringify + factorize instead of repeating it per detector.
_SCOPE: ContextVar[tuple[pd.Series, PreparedValues] | None] = ContextVar(
    "detector_prepared_scope", default=None
)


@contextmanager
def prepared_scope(series: pd.Series) -> Iterator[None]:
    """Share one PreparedValues for `series` across detector calls in the block."""
    scoped = _SCOPE.get()
    if scoped is not None and scoped[0] is series:
        yield
        return
    token = _SCOPE.set((series, PreparedValues(series)))
    try:
        yield
    finally:
        _SCOPE.reset(token)


class DetectorCount(NamedTuple):
    """One detector's tally over one column before the threshold test."""

    detector_id: str
    hits: int
    n: int
    threshold: float
    name_hint: bool


_COUNTS: ContextVar[list[DetectorCount] | None] = ContextVar("detector_count_scope", default=None)


@contextmanager
def counting_scope() -> Iterator[list[DetectorCount]]:
    """Collect every detector's raw tally in the block, whether or not it fired.

    A caller that streams a column sums hits and n over its batches and applies the
    threshold itself, so the rate is the whole-column rate and not an average of batch rates."""
    counts: list[DetectorCount] = []
    token = _COUNTS.set(counts)
    try:
        yield counts
    finally:
        _COUNTS.reset(token)


def _series_str(series: pd.Series) -> PreparedValues:
    """Drop nulls, stringify and strip, once per column inside a `prepared_scope`."""
    scoped = _SCOPE.get()
    if scoped is not None and scoped[0] is series:
        return scoped[1]
    return PreparedValues(series)


def _confidence_bucket(rate: float, *, name_hint: bool) -> str:
    """Classify a firing detector's confidence into the 3-bucket model.

    The UI keys off this label, not match_rate, so the bucket boundaries live
    in one place.
    """
    if (
        name_hint and rate >= HIGH_CONFIDENCE_WITH_HINT_FLOOR
    ) or rate >= HIGH_CONFIDENCE_NO_HINT_FLOOR:
        return "high"
    if (not name_hint and rate >= 0.50) or (name_hint and rate >= NAME_HINT_MIN_RATE):
        return "medium"
    return "low"


def _dominant_variant(
    uniques: pd.Series,
    counts: np.ndarray,
    variants: list[tuple[str, re.Pattern[str]]],
) -> str | None:
    """Label of the variant that matches the most cells.

    Variants are tested in order; each value is counted under the first variant
    that fullmatches it. Returns the highest-count label, or None when no
    variant matched any value.
    """
    if len(uniques) == 0:
        return None
    remaining = uniques
    remaining_counts = counts
    tally: dict[str, int] = {}
    for label, pat in variants:
        if len(remaining) == 0:
            break
        hit = remaining.str.fullmatch(pat).to_numpy(dtype=bool)
        n = int(remaining_counts[hit].sum())
        if n > 0:
            tally[label] = tally.get(label, 0) + n
            keep = ~hit
            remaining = remaining[keep]
            remaining_counts = remaining_counts[keep]
    if not tally:
        return None
    return max(tally.items(), key=lambda kv: kv[1])[0]


def _evaluate(
    detector_id: str,
    values: PreparedValues,
    pattern: re.Pattern[str],
    *,
    name_hint: bool,
    min_rate: float,
    validator: Callable[[str], bool] | None = None,
    format_variants: list[tuple[str, re.Pattern[str]]] | None = None,
) -> DetectorMatch | None:
    """Apply a regex to non-null values and decide whether the detector fires.

    Optional `validator` runs per distinct value AFTER the regex match (Luhn for
    PAN, mod-97 for IBAN, octet range for IPv4, and so on). A value counts as a
    match only if the regex matches AND the validator returns True.

    ``format_variants`` is an ordered list of ``(label, sub_pattern)`` pairs.
    The dominant matching variant's label goes to ``DetectorMatch.format_pattern``
    so the mask post-pass can splice separators back at the right positions.
    """
    if values.n == 0:
        return None
    uniques = values.uniques
    hit = uniques.str.fullmatch(pattern).to_numpy(dtype=bool)
    if validator is not None:
        # Only regex-passers reach the validator; a non-matching string cannot
        # pass a checksum either.
        validated = np.zeros(len(uniques), dtype=bool)
        for i in np.flatnonzero(hit):
            try:
                if validator(uniques.iat[i]):
                    validated[i] = True
            except (TypeError, ValueError):
                continue
        hit = validated
    hits = int(values.counts[hit].sum())
    rate = float(hits) / values.n
    threshold = NAME_HINT_MIN_RATE if name_hint else min_rate
    tally = _COUNTS.get()
    if tally is not None:
        tally.append(DetectorCount(detector_id, hits, values.n, threshold, name_hint))
    if rate < threshold:
        return None
    miss_rows = np.flatnonzero(~hit[values.codes])[:SAMPLE_MISS_LIMIT]
    misses = uniques.iloc[values.codes[miss_rows]].tolist()
    format_pattern: str | None = None
    if format_variants:
        format_pattern = _dominant_variant(uniques[hit], values.counts[hit], format_variants)
    return DetectorMatch(
        detector_id=detector_id,
        match_rate=round(rate, 4),
        sample_misses=[str(m) for m in misses],
        format_pattern=format_pattern,
        confidence=_confidence_bucket(rate, name_hint=name_hint),
        domain=domain_for(detector_id).value,
    )

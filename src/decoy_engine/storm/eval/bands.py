"""Confidence-band definitions and per-column latency benchmark (ML0.3).

Defines the three operational confidence bands (high / review / low) for
STORM field-recognition suggestions, and provides a micro-benchmark for
measuring per-column detection latency against the <50ms dev-tier budget.

Band thresholds (source: ml-benchmarking-and-privacy.md §A.4, superseding
ml0.3-confidence-bands.md):

    high   : precision >= 0.95  -- suggestion can be accepted with low review cost
    review : 0.70 <= precision < 0.95  -- human must confirm before accepting
    low    : precision < 0.70   -- suggestion should be treated as a hint only

These thresholds are defined on the REGEX BASELINE precision (measured by
``run_baseline()``), not on calibrated model probabilities.

CALIBRATION DEFERRED (ML2)
---------------------------
For a probabilistic model (ML2.2, LightGBM), the bands MUST be applied to
CALIBRATED ``predict_proba`` scores, not raw model outputs:

    - Report a reliability curve + Brier score for the shipped model.
    - Use ``CalibratedClassifierCV(method='isotonic')`` for >= ~1000
      calibration samples; sigmoid otherwise.
    - Set the high/review/low thresholds on calibrated scores so that a
      "high confidence >= X" band means ~X% of those predictions are
      correct (verified on the held-out set).

This requirement is deferred because the regex baseline has no
``predict_proba``. The threshold names and documentation are established
here so ML2.2 inherits the correct interface.

Source: sklearn §1.16 (reliability diagrams, Brier score, isotonic >= ~1000
samples); ml-benchmarking-and-privacy.md §A.4.
"""

from __future__ import annotations

import time
from typing import Literal

import pandas as pd

# ── Band thresholds (§A.4) ────────────────────────────────────────────────────

#: Precision floor for the "high" confidence band: >= HIGH_PRECISION_FLOOR.
HIGH_PRECISION_FLOOR: float = 0.95

#: Precision floor for the "review" band: >= REVIEW_PRECISION_FLOOR and
#: < HIGH_PRECISION_FLOOR.  Below REVIEW_PRECISION_FLOOR -> "low" band.
REVIEW_PRECISION_FLOOR: float = 0.70

#: Per-column synchronous detection budget on the dev-machine tier.
#: Source: ml0.3-confidence-bands.md and ml-benchmarking-and-privacy.md.
LATENCY_BUDGET_MS: float = 50.0

Band = Literal["high", "review", "low"]


def classify_band(precision: float) -> Band:
    """Classify a precision score into the high / review / low confidence band.

    Parameters
    ----------
    precision:
        Measured or calibrated precision for one semantic type, in [0, 1].

    Returns
    -------
    Band
        ``"high"`` if precision >= 0.95,
        ``"review"`` if 0.70 <= precision < 0.95,
        ``"low"`` otherwise.

    Notes
    -----
    For the regex baseline, pass ``rep.by_field_type[type_id].precision``.
    For a probabilistic model (ML2), pass the CALIBRATED score -- see the
    module docstring for the calibration requirement.
    """
    if precision >= HIGH_PRECISION_FLOOR:
        return "high"
    if precision >= REVIEW_PRECISION_FLOOR:
        return "review"
    return "low"


def benchmark_column_latency(
    series: pd.Series,
    column_name: str,
    *,
    n_warmup: int = 3,
    n_timed: int = 10,
) -> float:
    """Return mean per-column detection latency in milliseconds.

    Runs ``run_all_detectors`` against ``series`` / ``column_name``,
    discarding ``n_warmup`` runs to prime caches, then timing ``n_timed``
    runs and returning the mean elapsed time in milliseconds.

    Parameters
    ----------
    series:
        The pandas Series to detect on.
    column_name:
        Column name passed to the detector (drives name-hint signals).
    n_warmup:
        Warm-up iterations (default 3).  Not counted in the mean.
    n_timed:
        Timed iterations (default 10).  Mean of these is returned.

    Returns
    -------
    float
        Mean latency per run in milliseconds.

    Raises
    ------
    AssertionError
        Not raised here; callers (tests) compare the return value against
        ``LATENCY_BUDGET_MS`` and fail if exceeded.
    """
    from decoy_engine.storm.detectors import run_all_detectors

    for _ in range(n_warmup):
        run_all_detectors(series, column_name)

    times: list[float] = []
    for _ in range(n_timed):
        t0 = time.perf_counter()
        run_all_detectors(series, column_name)
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1000.0)

    return sum(times) / len(times)


def benchmark_all_fixture_columns(
    fixtures: list[object] | None = None,
) -> dict[str, float]:
    """Return a {fixture.column -> mean_ms} timing map over all fixture columns.

    Used by the CI latency gate to verify that no column exceeds
    ``LATENCY_BUDGET_MS`` (50 ms) on the dev-machine tier.

    Parameters
    ----------
    fixtures:
        A list of ``LabeledFixture`` objects.  Defaults to the standard
        five-fixture corpus from ``build_fixtures()``.

    Returns
    -------
    dict mapping "fixture.column" keys to mean latency in milliseconds.
    """
    from decoy_engine.storm.eval.fixtures import build_fixtures

    if fixtures is None:
        fixtures = build_fixtures()  # type: ignore[assignment]

    results: dict[str, float] = {}
    for fx in fixtures:  # type: ignore[union-attr]
        for col in fx.df.columns:  # type: ignore[union-attr]
            key = f"{fx.name}.{col}"  # type: ignore[union-attr]
            results[key] = benchmark_column_latency(fx.df[col], col)  # type: ignore[union-attr]
    return results

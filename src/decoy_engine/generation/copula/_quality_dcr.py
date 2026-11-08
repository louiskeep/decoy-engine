"""Distance to closest record (DCR) for copula output, summarized behind a percentile floor.

Pattern: SDMetrics DCR (calculate_dcr, DCROverfittingProtection; sdv-dev SDMetrics, Business
Source License, method only) over the Gower 1971 mixed-type distance. See:
docs/plans/2026-11-07-v2-4.4-copula-quality-report.md.

Per cell: numeric `min(|a - b| / range, 1)`, or `0 if a == b else 1` for a zero range; categorical
`0 if a == b else 1`; both null 0, exactly one null 1, in every kind. A row's distance is the mean
over columns and its DCR the minimum over reference rows. The range is the real column's span (the
model's edges) for every reference frame, so distances to the real and to a holdout share one unit.
Deviations from SDMetrics, each for a stated reason: the overfitting share compares equal-sized
real and holdout samples (a larger reference has smaller nearest distances), and samples are
systematic, not random.

The baseline is each real row's distance to its nearest other real row (table-GAN, Park et al.,
PVLDB 2018). A percentile q is released only when `min(q, 1 - q) * rows >= MIN_DISTINGUISHED_ROWS`:
a tail percentile of a small block is the distance of a few isolated rows.

Memory is O(block * reference rows): query rows are processed in blocks, never a full
query-by-reference matrix.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
import pandas as pd

from decoy_engine.generation.copula import _floors
from decoy_engine.generation.copula._model import KIND_EMPTY, KIND_NUMERIC, GaussianCopulaModel
from decoy_engine.generation.copula._quality_inputs import sample_rows

FloatArray = npt.NDArray[np.float64]
BoolArray = npt.NDArray[np.bool_]

BLOCK_ROWS = 256
WITHHELD_PERCENTILE = "percentile_rows"
# (name, percent): integer percents keep the floor test exact.
PERCENTILES = (("p05", 5), ("p25", 25), ("median", 50), ("p75", 75), ("p95", 95))


@dataclass(frozen=True)
class DcrSummary:
    p05: float | None
    p25: float | None
    median: float | None
    p75: float | None
    p95: float | None
    rows: int
    withheld: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class HoldoutComparison:
    rows_compared: int
    closer_to_real_share: float
    protection_score: float
    synthetic_to_holdout: DcrSummary


@dataclass(frozen=True)
class DcrBlock:
    columns: tuple[str, ...]
    real_rows_sampled: int
    synthetic_rows_sampled: int
    synthetic_to_real: DcrSummary
    exact_match_share: float
    real_to_real: DcrSummary
    holdout: HoldoutComparison | None


@dataclass(frozen=True)
class DistanceSpec:
    """Columns compared, which are numeric, and each numeric column's range."""

    columns: tuple[str, ...]
    numeric: tuple[bool, ...]
    ranges: tuple[float, ...]


def _round(value: float) -> float:
    return round(float(value), _floors.RELEASE_DECIMALS) + 0.0


def spec_for(model: GaussianCopulaModel) -> DistanceSpec:
    kept = [c for c in model.columns if c.kind != KIND_EMPTY]
    return DistanceSpec(
        tuple(c.name for c in kept),
        tuple(c.kind == KIND_NUMERIC for c in kept),
        tuple(
            float(c.bin_edges[-1] - c.bin_edges[0]) if c.kind == KIND_NUMERIC else 0.0 for c in kept
        ),
    )


def encode(frames: list[pd.DataFrame], spec: DistanceSpec) -> list[FloatArray]:
    """Each canonical frame as a float matrix: numeric values, or codes of categorical labels over
    the union of all frames' labels (sorted, so the codes do not depend on row order); NaN null."""
    out: list[FloatArray] = [np.empty((len(f), len(spec.columns))) for f in frames]
    for j, name in enumerate(spec.columns):
        if spec.numeric[j]:
            for matrix, frame in zip(out, frames, strict=True):
                matrix[:, j] = frame[name].to_numpy(dtype=np.float64)
            continue
        labels = sorted({v for f in frames for v in f[name].to_numpy() if v is not None})
        codes = {label: float(i) for i, label in enumerate(labels)}
        for matrix, frame in zip(out, frames, strict=True):
            matrix[:, j] = [np.nan if v is None else codes[v] for v in frame[name].to_numpy()]
    return out


def nearest(
    query: FloatArray,
    reference: FloatArray,
    spec: DistanceSpec,
    *,
    exclude_self: bool = False,
    block: int = BLOCK_ROWS,
) -> FloatArray:
    """Each query row's minimum mean Gower distance to the reference rows. With `exclude_self`
    the query and reference are the same rows and a row is never its own neighbour."""
    columns = len(spec.columns)
    out = np.empty(query.shape[0])
    for start in range(0, query.shape[0], block):
        stop = min(start + block, query.shape[0])
        total = np.zeros((stop - start, reference.shape[0]))
        for j in range(columns):
            a = query[start:stop, j][:, None]
            b = reference[:, j][None, :]
            if spec.numeric[j] and spec.ranges[j] > 0:
                cell = np.minimum(np.abs(a - b) / spec.ranges[j], 1.0)
            else:
                cell = (a != b).astype(np.float64)
            missing_a, missing_b = np.isnan(a), np.isnan(b)
            cell = np.where(missing_a & missing_b, 0.0, np.where(missing_a | missing_b, 1.0, cell))
            total += cell
        if exclude_self:
            rows = np.arange(start, stop)
            total[rows - start, rows] = np.inf
        out[start:stop] = total.min(axis=1) / columns
    return out


def summarize(distances: FloatArray) -> DcrSummary:
    """Percentiles behind the floor: q is released when min(q, 1 - q) * rows reaches the floor."""
    rows = int(distances.size)
    values: dict[str, float | None] = {}
    withheld: list[tuple[str, str]] = []
    for name, percent in PERCENTILES:
        if rows and min(percent, 100 - percent) * rows >= _floors.MIN_DISTINGUISHED_ROWS * 100:
            values[name] = _round(float(np.quantile(distances, percent / 100)))
        else:
            values[name] = None
            withheld.append((name, WITHHELD_PERCENTILE))
    return DcrSummary(
        values["p05"],
        values["p25"],
        values["median"],
        values["p75"],
        values["p95"],
        rows=rows,
        withheld=tuple(withheld),
    )


def compute_dcr_block(
    real: pd.DataFrame,
    synthetic: pd.DataFrame,
    holdout: pd.DataFrame | None,
    model: GaussianCopulaModel,
    max_rows: int,
) -> DcrBlock | None:
    """All DCR outputs from canonical frames, every side capped at `max_rows` systematically."""
    spec = spec_for(model)
    if not spec.columns:
        return None
    k_r, k_s = min(len(real), max_rows), min(len(synthetic), max_rows)
    real_rows = sample_rows(len(real), k_r)
    frames = [real.iloc[real_rows], synthetic.iloc[sample_rows(len(synthetic), k_s)]]
    if holdout is not None:
        k_h = min(len(real), len(holdout), max_rows)
        frames.append(holdout.iloc[sample_rows(len(holdout), k_h)])
        frames.append(real.iloc[sample_rows(len(real), k_h)])
    encoded = encode(frames, spec)
    real_m, synth_m = encoded[0], encoded[1]
    to_real = nearest(synth_m, real_m, spec)
    comparison = None
    if holdout is not None:
        hold_m, real_h = encoded[2], encoded[3]
        to_real_h = to_real if real_h.shape[0] == k_r else nearest(synth_m, real_h, spec)
        to_hold = nearest(synth_m, hold_m, spec)
        share = float(np.mean(to_real_h < to_hold))
        comparison = HoldoutComparison(
            rows_compared=int(hold_m.shape[0]),
            closer_to_real_share=_round(share),
            protection_score=_round(min(2.0 * (1.0 - share), 1.0)),
            synthetic_to_holdout=summarize(to_hold),
        )
    return DcrBlock(
        columns=spec.columns,
        real_rows_sampled=k_r,
        synthetic_rows_sampled=k_s,
        synthetic_to_real=summarize(to_real),
        exact_match_share=_round(float(np.mean(to_real == 0.0))),
        real_to_real=summarize(nearest(real_m, real_m, spec, exclude_self=True)),
        holdout=comparison,
    )

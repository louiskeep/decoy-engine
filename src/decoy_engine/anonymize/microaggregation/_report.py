"""Value-free report of a microaggregation: group sizes and information loss.

Pattern: SSE/SST information loss (Domingo-Ferrer and Mateo-Sanz 2002)

J. Domingo-Ferrer and J. M. Mateo-Sanz, "Practical data-oriented
microaggregation for statistical disclosure control", IEEE TKDE 14(1), 2002:
on standardised data, SSE is the sum of squared distances of records to their
group centroid and SST to the overall centroid; ``SSE / SST`` is the share of
variance lost (0 keeps everything, 1 keeps nothing). Per column, ``SSE_j /
SST_j`` is that column's pooled within-group variance (divisor n) on the
standardised scale.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

FloatMatrix = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]


@dataclass(frozen=True, slots=True)
class MicroaggregationReport:
    """Group sizes and information loss; sizes are ``None`` for an empty table."""

    row_count: int
    k: int
    quasi_identifiers: tuple[str, ...]
    group_count: int
    group_size_min: int | None
    group_size_max: int | None
    group_size_distribution: tuple[tuple[int, int], ...]
    sse: float
    sst: float
    information_loss: float
    column_information_loss: tuple[float, ...]

    def __post_init__(self) -> None:
        counts = (self.row_count, self.k, self.group_count)
        if any(type(c) is not int or c < 0 for c in counts):
            raise ValueError("report counts must be non-negative ints")
        if any(type(n) is not str for n in self.quasi_identifiers):
            raise TypeError("report quasi-identifiers must be str")
        for value in (self.sse, self.sst):
            if type(value) is not float or not math.isfinite(value) or value < 0:
                raise ValueError("report sums of squares must be finite and non-negative")
        losses = (self.information_loss, *self.column_information_loss)
        if any(type(v) is not float or not 0.0 <= v <= 1.0 for v in losses):
            raise ValueError("report information loss must be between 0 and 1")


def group_means(values: FloatMatrix, groups: IntArray, count: int) -> FloatMatrix:
    """Per-group column means, members summed in ascending row order."""
    if count == 0 or values.shape[1] == 0:
        return np.zeros((count, values.shape[1]), dtype=np.float64)
    order = np.argsort(groups, kind="stable")
    sizes = np.bincount(groups, minlength=count)
    starts = np.concatenate([[0], np.cumsum(sizes)[:-1]])
    sums = np.add.reduceat(values[order], starts, axis=0)
    return sums / sizes[:, None]


def _loss(sse: float, sst: float) -> float:
    # The mean minimises the within-group sum, so sse <= sst; min() absorbs rounding only.
    return min(sse / sst, 1.0) if sst > 0 else 0.0


def build_report(
    z: FloatMatrix,
    constant: npt.NDArray[np.bool_],
    groups: IntArray,
    count: int,
    k: int,
    names: tuple[str, ...],
) -> MicroaggregationReport:
    rows = int(groups.size)
    sizes = np.bincount(groups, minlength=count)
    seen, how_many = np.unique(sizes, return_counts=True)
    residual = z - group_means(z, groups, count)[groups]
    sse_kept = (residual * residual).sum(axis=0)
    spread = z - z.mean(axis=0) if rows else z
    sst_kept = (spread * spread).sum(axis=0)
    sse_col = np.zeros(len(names))
    sst_col = np.zeros(len(names))
    sse_col[~constant] = sse_kept
    sst_col[~constant] = sst_kept
    sse = float(sse_col.sum())
    sst = float(sst_col.sum())
    return MicroaggregationReport(
        row_count=rows,
        k=k,
        quasi_identifiers=names,
        group_count=count,
        group_size_min=int(sizes.min()) if count else None,
        group_size_max=int(sizes.max()) if count else None,
        group_size_distribution=tuple(
            (int(s), int(c)) for s, c in zip(seen, how_many, strict=True)
        ),
        sse=sse,
        sst=sst,
        information_loss=_loss(sse, sst),
        column_information_loss=tuple(
            _loss(float(e), float(t)) for e, t in zip(sse_col, sst_col, strict=True)
        ),
    )

"""t-closeness: Earth Mover's Distance from each class to the whole table.

Pattern: t-closeness EMD (Li, Li and Venkatasubramanian 2007).

N. Li, T. Li and S. Venkatasubramanian, "t-Closeness: Privacy Beyond
k-Anonymity and l-Diversity", ICDE 2007. A class is t-close when the EMD
between its sensitive distribution P and the table's Q is at most t. Two of
the paper's ground distances (ARX's EqualDistance and OrderedDistance
models):

- equal: every pair of distinct values at distance 1, so
  ``EMD = 1/2 sum_v |p_v - q_v|`` (total variation);
- ordered: over the m observed values in sort order, ``d(v_i, v_j) =
  |i - j| / (m - 1)`` and ``EMD = 1/(m-1) sum_i |F(i) - G(i)|`` with F, G the
  cumulative class and table distributions (0 when m = 1).

The paper's hierarchical distance needs generalization hierarchies and is
not provided.

Evaluation is sparse and exact. With N non-null sensitive rows, n_c in the
class, and integer cumulative counts CP (class) and CQ (table),
``|F - G| = |CP*N - CQ*n_c| / (n_c*N)``. F is constant between the class's
values, and G is non-decreasing, so each constant segment [a, b) splits at
``s``, the first position with ``CQ*n_c >= CP*N``, and sums in closed form
from the integer prefix sums of CQ. Every term is an exact integer
(``int64`` when its bound allows, Python ints otherwise); the only rounding
is one division per class. Cost is O(P log m) for P non-zero (class, value)
pairs, never a classes-by-values matrix.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import numpy.typing as npt
import pandas as pd
import pyarrow as pa

from decoy_engine.quality.anonymity._classes import (
    Classes,
    IntArray,
    SensitiveCounts,
    check_columns,
    check_unit_interval,
    equivalence_classes,
    ordered_sensitive_codes,
    read_columns,
    rows_in,
    sensitive_counts,
    sensitive_family_codes,
)
from decoy_engine.quality.anonymity._report import check_report_values

GroundDistance = Literal["equal", "ordered"]
_GROUND_DISTANCES: frozenset[str] = frozenset({"equal", "ordered"})
_T_TOLERANCE = 1e-12
_INT64_SAFE = 2**62


@dataclass(frozen=True, slots=True)
class TClosenessReport:
    """Largest class EMD and target counts. ``meets_target`` is ``True`` only
    when every class was measured and none exceeds t; ``False`` when a
    measured class exceeds t; otherwise ``None`` (no target, no classes, or a
    class without non-null sensitive values)."""

    ground_distance: GroundDistance
    row_count: int
    class_count: int
    classes_measured: int
    classes_without_sensitive_values: int
    rows_in_unmeasured_classes: int
    rows_with_null_sensitive: int
    distinct_sensitive_values: int
    max_emd: float | None
    t_target: float | None
    classes_above_target: int | None
    rows_above_target: int | None
    meets_target: bool | None

    def __post_init__(self) -> None:
        check_report_values(self, _GROUND_DISTANCES)


def _exact_dtype(bound: int) -> type:
    # object arrays hold Python ints: exact at any size, only slower.
    return np.int64 if bound < _INT64_SAFE else object


def _divide(numerators: npt.NDArray, denominators: npt.NDArray) -> npt.NDArray[np.float64]:
    if numerators.dtype == object or denominators.dtype == object:
        return np.array(
            [int(a) / int(b) for a, b in zip(numerators, denominators, strict=True)],
            dtype=np.float64,
        )
    return numerators.astype(np.float64) / denominators.astype(np.float64)


def _equal_emd(counts: SensitiveCounts, measured: IntArray) -> npt.NDArray[np.float64]:
    """1/2 sum_v |c_v*N - Q_v*n_c| / (n_c*N); absent values add Q_v*n_c each,
    which sums to N*n_c minus the present Q_v*n_c."""
    total = int(counts.value_totals.sum())
    dtype = _exact_dtype(4 * total * total)
    n_c: npt.NDArray[Any] = counts.class_totals.astype(dtype)
    cls = counts.pair_class
    c_v: npt.NDArray[Any] = counts.pair_count.astype(dtype)
    q_v: npt.NDArray[Any] = counts.value_totals[counts.pair_value].astype(dtype)
    nc_pair = n_c[cls]
    present = np.abs(c_v * total - q_v * nc_pair) - q_v * nc_pair
    sums: npt.NDArray[Any] = np.zeros(counts.class_totals.shape[0], dtype=dtype)
    np.add.at(sums, cls, present)
    numerator = sums[measured] + n_c[measured] * total
    return _divide(numerator, 2 * n_c[measured] * total)


def _ordered_emd(counts: SensitiveCounts, measured: IntArray) -> npt.NDArray[np.float64]:
    m = counts.value_count
    if m <= 1:
        return np.zeros(measured.shape[0], dtype=np.float64)
    total = int(counts.value_totals.sum())
    nc_max = int(counts.class_totals.max())
    dtype = _exact_dtype(2 * nc_max * total * (m + 1) + 1)
    cq = np.cumsum(counts.value_totals).astype(np.int64)
    pcq: npt.NDArray[Any] = np.concatenate(([0], np.cumsum(cq))).astype(dtype)

    # Segments: per measured class a head [0, first value) with CP = 0, then
    # one segment per present value up to the next present value (or m).
    cls = counts.pair_class
    pos = counts.pair_value
    starts = np.flatnonzero(np.r_[True, cls[1:] != cls[:-1]])
    class_start = np.repeat(starts, np.diff(np.r_[starts, cls.size]))
    running = np.cumsum(counts.pair_count)
    cp = running - (running[class_start] - counts.pair_count[class_start])
    nxt = np.r_[pos[1:], m]
    last = np.r_[cls[1:] != cls[:-1], True]
    seg_end = np.where(last, m, nxt)

    seg_class = np.concatenate((cls[starts], cls))
    seg_a = np.concatenate((np.zeros(starts.size, dtype=np.int64), pos))
    seg_b = np.concatenate((pos[starts], seg_end))
    seg_cp = np.concatenate((np.zeros(starts.size, dtype=np.int64), cp))

    n_c: npt.NDArray[Any] = counts.class_totals.astype(dtype)
    seg_nc = n_c[seg_class]
    cp_n: npt.NDArray[Any] = seg_cp.astype(dtype) * total
    # First position where CQ*n_c >= CP*N, i.e. CQ >= ceil(CP*N / n_c); the
    # threshold is at most N, so it fits int64 whatever the arithmetic type.
    threshold = (-((-cp_n) // seg_nc)).astype(np.int64)
    s = np.clip(np.searchsorted(cq, threshold, side="left"), seg_a, seg_b)

    a: npt.NDArray[Any] = seg_a.astype(dtype)
    b = seg_b.astype(dtype)
    sd = s.astype(dtype)
    bracket = (
        cp_n * (sd - a)
        - seg_nc * (pcq[s] - pcq[seg_a])
        + seg_nc * (pcq[seg_b] - pcq[s])
        - cp_n * (b - sd)
    )
    sums: npt.NDArray[Any] = np.zeros(counts.class_totals.shape[0], dtype=dtype)
    np.add.at(sums, seg_class, bracket)
    return _divide(sums[measured], n_c[measured] * total * (m - 1))


@dataclass(frozen=True)
class _GroundDistanceKernel:
    """Everything one ground distance decides: how sensitive cells are coded,
    how the EMD is evaluated, and whether category order is read."""

    name: GroundDistance
    codes_of: Callable[[pd.Series, str], IntArray]
    emd_of: Callable[[SensitiveCounts, IntArray], npt.NDArray[np.float64]]
    reads_order: bool


def _measure(
    table: pd.DataFrame | pa.Table,
    quasi_identifiers: Sequence[str],
    sensitive: str,
    t_target: float | None,
    kernel: _GroundDistanceKernel,
) -> TClosenessReport:
    columns = check_columns(table, quasi_identifiers, sensitive)
    t = check_unit_interval("t_target", t_target)
    frame = read_columns(
        table, columns, frozenset({sensitive}) if kernel.reads_order else frozenset()
    )
    codes = kernel.codes_of(frame.iloc[:, -1], sensitive)
    classes = equivalence_classes(frame, columns[:-1])
    counts = sensitive_counts(classes, codes)
    unmeasured = counts.class_totals == 0
    measured = np.flatnonzero(~unmeasured).astype(np.int64)
    emds = kernel.emd_of(counts, measured) if measured.size else np.zeros(0)

    above: int | None = None
    above_rows: int | None = None
    meets: bool | None = None
    if t is not None:
        over = np.zeros(classes.count, dtype=bool)
        over[measured] = emd_above(emds, t)
        above = int(over.sum())
        above_rows = rows_in(classes, over)
        meets = _verdict(classes, above, int(unmeasured.sum()))

    return TClosenessReport(
        ground_distance=kernel.name,
        row_count=int(classes.sizes.sum()),
        class_count=classes.count,
        classes_measured=int(measured.size),
        classes_without_sensitive_values=int(unmeasured.sum()),
        rows_in_unmeasured_classes=rows_in(classes, unmeasured),
        rows_with_null_sensitive=counts.rows_with_null,
        distinct_sensitive_values=counts.value_count,
        max_emd=float(emds.max()) if emds.size else None,
        t_target=t,
        classes_above_target=above,
        rows_above_target=above_rows,
        meets_target=meets,
    )


def emd_above(emds: npt.NDArray[np.float64], t: float) -> npt.NDArray[np.bool_]:
    """Measured classes farther than ``t`` from the table, beyond the tolerance."""
    return emds > t + _T_TOLERANCE


def _verdict(classes: Classes, above: int, unmeasured: int) -> bool | None:
    if above:
        return False
    if unmeasured or classes.count == 0:
        return None
    return True


_EQUAL = _GroundDistanceKernel("equal", sensitive_family_codes, _equal_emd, reads_order=False)
_ORDERED = _GroundDistanceKernel("ordered", ordered_sensitive_codes, _ordered_emd, reads_order=True)


def measure_t_closeness_equal(
    table: pd.DataFrame | pa.Table,
    quasi_identifiers: Sequence[str],
    sensitive: str,
    *,
    t_target: float | None = None,
) -> TClosenessReport:
    """t-closeness with equal ground distance (total variation); any column type.

    Sensitive cells are keyed by type family, as quasi-identifiers are:
    merging ``1``, ``1.0`` and ``True`` into one value could only lower the
    distance."""
    return _measure(table, quasi_identifiers, sensitive, t_target, _EQUAL)


def measure_t_closeness_ordered(
    table: pd.DataFrame | pa.Table,
    quasi_identifiers: Sequence[str],
    sensitive: str,
    *,
    t_target: float | None = None,
) -> TClosenessReport:
    """t-closeness with ordered ground distance. ``sensitive`` must be numeric,
    decimal, temporal, or an ordered categorical / ordered Arrow dictionary."""
    return _measure(table, quasi_identifiers, sensitive, t_target, _ORDERED)

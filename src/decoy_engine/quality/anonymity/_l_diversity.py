"""l-diversity of one sensitive column within each equivalence class.

Pattern: l-diversity (Machanavajjhala et al. 2007).

Definitions from A. Machanavajjhala, D. Kifer, J. Gehrke and M.
Venkitasubramaniam, "l-Diversity: Privacy Beyond k-Anonymity", ACM TKDD
1(1), 2007, as implemented under the same names by the ARX Data
Anonymization Tool. Per class, with d distinct non-null sensitive values,
counts ``r_1 >= ... >= r_d`` and ``n_c = sum r_i``:

- distinct l = d;
- entropy l = ``exp(H)``, ``H = -sum (r_i/n_c) ln(r_i/n_c)``; the class meets
  l when ``H >= ln l`` (within ``1e-12`` so a class of l equally frequent
  values is not failed by rounding);
- recursive l at c = the largest l in ``1..d`` with
  ``r_1 < c * (r_l + ... + r_d)``, 0 when none holds. The predicate is
  monotone in l, so the class meets (c, l) when this is at least l. A value of
  1 is this module's convention (the paper defines l >= 2): it means
  ``r_1 < c * n_c``.

Table measures are minima over classes. A class with no non-null sensitive
value has all three measures 0.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
import pandas as pd
import pyarrow as pa

from decoy_engine.quality.anonymity._classes import (
    Classes,
    SensitiveCounts,
    check_columns,
    check_int_target,
    check_positive_real,
    equivalence_classes,
    read_columns,
    rows_in,
    sensitive_codes,
    sensitive_counts,
)
from decoy_engine.quality.anonymity._report import check_report_values

_ENTROPY_TOLERANCE = 1e-12


@dataclass(frozen=True, slots=True)
class LDiversityReport:
    """Per-variant minima and target counts. Recursive fields are ``None``
    without ``recursive_c``; target fields are ``None`` without ``l_target``
    (and, for recursive, without ``recursive_c``); ``meets_*`` is ``None`` for
    an empty table."""

    row_count: int
    class_count: int
    rows_with_null_sensitive: int
    classes_without_sensitive_values: int
    distinct_l_min: int
    entropy_l_min: float
    recursive_c: float | None
    recursive_l_min: int | None
    l_target: int | None
    distinct_classes_below_target: int | None
    distinct_rows_below_target: int | None
    distinct_meets_target: bool | None
    entropy_classes_below_target: int | None
    entropy_rows_below_target: int | None
    entropy_meets_target: bool | None
    recursive_classes_below_target: int | None
    recursive_rows_below_target: int | None
    recursive_meets_target: bool | None

    def __post_init__(self) -> None:
        check_report_values(self)


def _entropies(counts: SensitiveCounts, n_classes: int) -> npt.NDArray[np.float64]:
    totals = counts.class_totals[counts.pair_class].astype(np.float64)
    p = counts.pair_count / totals
    entropy = np.bincount(counts.pair_class, weights=-p * np.log(p), minlength=n_classes)
    return entropy.astype(np.float64)


def _recursive_levels(counts: SensitiveCounts, n_classes: int, c: float) -> npt.NDArray[np.int64]:
    order = np.lexsort((-counts.pair_count, counts.pair_class))
    cls = counts.pair_class[order]
    r = counts.pair_count[order]
    starts = np.flatnonzero(np.r_[True, cls[1:] != cls[:-1]]) if cls.size else cls
    first = np.repeat(r[starts], np.diff(np.r_[starts, cls.size]))
    before = np.cumsum(r) - r
    before_class = np.repeat(before[starts], np.diff(np.r_[starts, cls.size]))
    # Tail sum from this rank on: n_c minus the counts ranked above it.
    tail = counts.class_totals[cls] - (before - before_class)
    holds = first < c * tail
    # The predicate holds on a prefix of ranks, so its count is the largest l.
    return np.bincount(cls, weights=holds, minlength=n_classes).astype(np.int64)


def _below(classes: Classes, failing: npt.NDArray[np.bool_]) -> tuple[int, int, bool | None]:
    count = int(failing.sum())
    return count, rows_in(classes, failing), (count == 0) if classes.count else None


def measure_l_diversity(
    table: pd.DataFrame | pa.Table,
    quasi_identifiers: Sequence[str],
    sensitive: str,
    *,
    l_target: int | None = None,
    recursive_c: float | None = None,
) -> LDiversityReport:
    """Measure distinct, entropy and (with ``recursive_c``) recursive (c, l)
    l-diversity of ``sensitive`` over the classes of ``quasi_identifiers``.

    Null sensitive values are excluded; see the package docstring.
    """
    columns = check_columns(table, quasi_identifiers, sensitive)
    target = check_int_target("l_target", l_target)
    c = check_positive_real("recursive_c", recursive_c)
    frame = read_columns(table, columns)
    qi = columns[:-1]
    classes = equivalence_classes(frame, qi)
    counts = sensitive_counts(classes, sensitive_codes(frame.iloc[:, -1], sensitive))
    n = classes.count

    distinct = np.bincount(counts.pair_class, minlength=n).astype(np.int64)
    empty = counts.class_totals == 0
    entropy = _entropies(counts, n)
    entropy_l = np.where(empty, 0.0, np.exp(entropy))
    levels = _recursive_levels(counts, n, c) if c is not None else None

    fields: dict[str, int | bool | None] = {}
    for variant in ("distinct", "entropy", "recursive"):
        for suffix in ("classes_below_target", "rows_below_target", "meets_target"):
            fields[f"{variant}_{suffix}"] = None
    if target is not None:
        failing = {
            "distinct": distinct < target,
            "entropy": empty | (entropy < math.log(target) - _ENTROPY_TOLERANCE),
        }
        if levels is not None:
            failing["recursive"] = levels < target
        for variant, mask in failing.items():
            below, rows, meets = _below(classes, mask)
            fields[f"{variant}_classes_below_target"] = below
            fields[f"{variant}_rows_below_target"] = rows
            fields[f"{variant}_meets_target"] = meets

    return LDiversityReport(
        row_count=int(classes.sizes.sum()),
        class_count=n,
        rows_with_null_sensitive=counts.rows_with_null,
        classes_without_sensitive_values=int(empty.sum()),
        distinct_l_min=int(distinct.min()) if n else 0,
        entropy_l_min=float(entropy_l.min()) if n else 0.0,
        recursive_c=c,
        recursive_l_min=(int(levels.min()) if n else 0) if levels is not None else None,
        l_target=target,
        **fields,  # type: ignore[arg-type]
    )

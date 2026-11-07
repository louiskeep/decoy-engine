"""``microaggregate``: check, read, partition, replace by centroids, report."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
import pandas as pd
import pyarrow as pa

from decoy_engine.anonymize.microaggregation._inputs import (
    check_k,
    check_quasi_identifiers,
    check_row_count,
    check_table,
    read_matrix,
    row_count,
    write_table,
)
from decoy_engine.anonymize.microaggregation._mdav import mdav_groups, standardise
from decoy_engine.anonymize.microaggregation._report import (
    MicroaggregationReport,
    build_report,
    group_means,
)

FloatMatrix = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]


@dataclass(frozen=True, slots=True)
class MicroaggregationResult:
    """The new table (same type as the input) and its report."""

    table: pd.DataFrame | pa.Table
    report: MicroaggregationReport


def partition(
    x: FloatMatrix, names: Sequence[str], k: int
) -> tuple[IntArray, int, FloatMatrix, npt.NDArray[np.bool_]]:
    """Return ``(group ids, group count, z, constant mask)`` for a checked matrix."""
    z, constant = standardise(x, names)
    groups = mdav_groups(z, k)
    count = int(groups.max()) + 1 if groups.size else 0
    return groups, count, z, constant


def _centroid_rows(
    x: FloatMatrix, groups: IntArray, count: int, constant: npt.NDArray[np.bool_]
) -> FloatMatrix:
    out = x.copy()
    # One centroid vector per group, expanded by index, so members share its bits.
    with np.errstate(over="ignore", invalid="ignore"):
        centroids = group_means(x[:, ~constant], groups, count)
    if not np.isfinite(centroids).all():
        raise ValueError("quasi-identifier values are too large to aggregate")
    # Constant columns are written back as they are: their exact value, not a computed mean.
    out[:, ~constant] = centroids[groups]
    return out


def microaggregate(
    table: pd.DataFrame | pa.Table,
    quasi_identifiers: Sequence[str],
    *,
    k: int,
) -> MicroaggregationResult:
    """Replace ``quasi_identifiers`` by MDAV group means over groups of k to 2k-1 rows.

    See the package docstring for the contract, refusals and limits.
    """
    checked = check_table(table)
    size = check_k(k)
    names = check_quasi_identifiers(checked, quasi_identifiers)
    check_row_count(row_count(checked), size)
    x = read_matrix(checked, names)
    groups, count, z, constant = partition(x, names, size)
    values = _centroid_rows(x, groups, count, constant)
    report = build_report(z, constant, groups, count, size, names)
    return MicroaggregationResult(table=write_table(checked, names, values), report=report)

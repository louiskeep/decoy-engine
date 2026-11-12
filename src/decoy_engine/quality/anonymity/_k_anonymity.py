"""k-anonymity of a table over named quasi-identifiers.

Pattern: k-anonymity (Sweeney 2002) with ARX prosecutor risk.

A table is k-anonymous when every equivalence class (rows sharing all
quasi-identifier values) has at least k rows (L. Sweeney, "k-anonymity: a
model for protecting privacy", IJUFKS 10(5), 2002). The report adds the two
prosecutor-model figures the ARX Data Anonymization Tool derives from class
sizes alone: highest risk ``1 / min class size`` and average risk
``classes / rows``. Journalist and marketer risk need a population table and
are not computed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd
import pyarrow as pa

from decoy_engine.quality.anonymity._classes import (
    check_columns,
    check_int_target,
    equivalence_classes,
    read_columns,
    rows_in,
)
from decoy_engine.quality.anonymity._report import check_report_values
from decoy_engine.quality.anonymity._risk import prosecutor_average, prosecutor_highest


@dataclass(frozen=True, slots=True)
class KAnonymityReport:
    """Class-size measures; ``None`` where the table is empty or no target was set."""

    row_count: int
    class_count: int
    k_min: int | None
    class_size_distribution: tuple[tuple[int, int], ...]
    rows_with_null_quasi_identifier: int
    highest_prosecutor_risk: float | None
    average_prosecutor_risk: float | None
    k_target: int | None
    classes_below_target: int | None
    rows_below_target: int | None
    meets_target: bool | None

    def __post_init__(self) -> None:
        check_report_values(self)


def measure_k_anonymity(
    table: pd.DataFrame | pa.Table,
    quasi_identifiers: Sequence[str],
    *,
    k_target: int | None = None,
) -> KAnonymityReport:
    """Measure k-anonymity over ``quasi_identifiers``.

    A null quasi-identifier is its own value (see the package docstring).
    With ``k_target``, classes smaller than it and their rows are counted;
    ``meets_target`` is ``None`` for an empty table.
    """
    columns = check_columns(table, quasi_identifiers, None)
    target = check_int_target("k_target", k_target)
    classes = equivalence_classes(read_columns(table, columns), columns)
    sizes = classes.sizes
    rows = int(sizes.sum())
    count = classes.count
    sizes_seen, how_many = np.unique(sizes, return_counts=True)
    distribution = tuple((int(s), int(c)) for s, c in zip(sizes_seen, how_many, strict=True))
    k_min = int(sizes.min()) if count else None

    below: int | None = None
    below_rows: int | None = None
    meets: bool | None = None
    if target is not None:
        small = sizes < target
        below = int(small.sum())
        below_rows = rows_in(classes, small)
        meets = (below == 0) if count else None

    return KAnonymityReport(
        row_count=rows,
        class_count=count,
        k_min=k_min,
        class_size_distribution=distribution,
        rows_with_null_quasi_identifier=classes.rows_with_null_quasi_identifier,
        highest_prosecutor_risk=prosecutor_highest(k_min) if k_min else None,
        average_prosecutor_risk=prosecutor_average(count, rows) if rows else None,
        k_target=target,
        classes_below_target=below,
        rows_below_target=below_rows,
        meets_target=meets,
    )

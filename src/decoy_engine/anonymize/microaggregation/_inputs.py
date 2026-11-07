"""Argument checks, reading the quasi-identifier matrix, and writing the output table."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeAlias

import numpy as np
import numpy.typing as npt
import pandas as pd
import pyarrow as pa

FloatMatrix = npt.NDArray[np.float64]

# MDAV costs O(n^2 * p / k); this bounds one call at under a minute at k = 2, p = 4.
MAX_ROWS = 50_000

Table: TypeAlias = pd.DataFrame | pa.Table


def check_table(table: object) -> Table:
    if not isinstance(table, (pd.DataFrame, pa.Table)):
        raise TypeError("table must be a pandas DataFrame or a pyarrow Table")
    return table


def check_k(k: object) -> int:
    if type(k) is not int:
        raise TypeError("k must be an int (not bool, float or str)")
    if k < 2:
        raise ValueError("k must be at least 2")
    return k


def check_quasi_identifiers(table: Table, quasi_identifiers: object) -> tuple[str, ...]:
    if not isinstance(quasi_identifiers, (list, tuple)):
        raise TypeError("quasi_identifiers must be a list or tuple of column names")
    if not quasi_identifiers:
        raise ValueError("quasi_identifiers must name at least one column")
    names: list[str] = []
    for name in quasi_identifiers:
        if type(name) is not str:
            raise TypeError("each quasi-identifier must be a str")
        if name in names:
            raise ValueError(f"column {name!r} is named more than once in quasi_identifiers")
        names.append(name)
    present = list(table.columns) if isinstance(table, pd.DataFrame) else table.column_names
    for name in names:
        occurrences = present.count(name)
        if occurrences == 0:
            raise ValueError(f"column {name!r} is not in the table")
        if occurrences > 1:
            raise ValueError(f"column {name!r} appears more than once in the table")
    return tuple(names)


def check_row_count(rows: int, k: int) -> None:
    if rows > MAX_ROWS:
        raise ValueError(f"table has {rows} rows; microaggregation supports at most {MAX_ROWS}")
    if 0 < rows < k:
        raise ValueError("table has fewer rows than k")


def row_count(table: Table) -> int:
    return len(table) if isinstance(table, pd.DataFrame) else table.num_rows


def _pandas_column(table: pd.DataFrame, name: str) -> tuple[FloatMatrix, npt.NDArray[np.bool_]]:
    series = table[name]
    # kind is "i"/"u"/"f" for numpy, nullable and ArrowDtype numbers alike, and never for
    # bool ("b"), category, string, decimal or object ("O"), datetime ("M") or complex ("c").
    if getattr(series.dtype, "kind", "O") not in "iuf":
        raise TypeError(f"column {name!r} is not a numeric column")
    missing = series.isna().to_numpy(dtype=bool)
    values = series.to_numpy(dtype=np.float64, na_value=np.nan)
    return values, missing


def _arrow_column(table: pa.Table, name: str) -> tuple[FloatMatrix, npt.NDArray[np.bool_]]:
    column = table.column(name)
    if not (pa.types.is_integer(column.type) or pa.types.is_floating(column.type)):
        raise TypeError(f"column {name!r} is not a numeric column")
    as_float = column.cast(pa.float64(), safe=False)
    missing = as_float.is_null().to_numpy(zero_copy_only=False)
    values = as_float.fill_null(np.nan).to_numpy(zero_copy_only=False).astype(np.float64)
    return values, np.asarray(missing, dtype=bool)


def read_matrix(table: Table, names: Sequence[str]) -> FloatMatrix:
    """Return the quasi-identifiers as an (n, p) float64 matrix, refusing nulls and inf."""
    rows = row_count(table)
    matrix = np.empty((rows, len(names)), dtype=np.float64)
    null_rows = np.zeros(rows, dtype=bool)
    for j, name in enumerate(names):
        if isinstance(table, pd.DataFrame):
            values, missing = _pandas_column(table, name)
        else:
            values, missing = _arrow_column(table, name)
        matrix[:, j] = values
        null_rows |= missing | np.isnan(values)
    nulls = int(null_rows.sum())
    if nulls:
        raise ValueError(
            f"{nulls} rows have a null quasi-identifier; microaggregation needs complete values"
        )
    infinite = int((~np.isfinite(matrix)).any(axis=1).sum())
    if infinite:
        raise ValueError(f"{infinite} rows have a non-finite quasi-identifier")
    return matrix


def write_table(table: Table, names: Sequence[str], values: FloatMatrix) -> Table:
    """A new table with each named column replaced by float64 ``values``; input untouched."""
    if isinstance(table, pd.DataFrame):
        out = table.copy(deep=False)
        for j, name in enumerate(names):
            out[name] = pd.Series(values[:, j].copy(), index=table.index, name=name)
        return out
    result: pa.Table = table
    for j, name in enumerate(names):
        position = result.column_names.index(name)
        result = result.set_column(
            position, pa.field(name, pa.float64()), pa.array(values[:, j], type=pa.float64())
        )
    return result

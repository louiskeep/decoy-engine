"""Column builders shared by the hierarchy tests."""

from __future__ import annotations

from typing import Any

import pandas as pd
import pyarrow as pa


def obj(values: list[Any]) -> pd.Series:
    """A pandas object column holding exactly these cells."""
    return pd.Series(values, dtype=object)


def labels(column: Any) -> list[Any]:
    """Output cells as a plain list (pandas or Arrow)."""
    if isinstance(column, pd.Series):
        return [None if v is None else v for v in column.tolist()]
    assert isinstance(column, pa.Array)
    return column.to_pylist()

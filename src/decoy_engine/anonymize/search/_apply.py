"""Apply a search result: generalize to the chosen node and suppress the outliers.

The outliers are recomputed from the generalized table itself (classes over
its labels, through ``FrequencySet``), not taken from the search's rollup, so
a result is only applied to a table it was computed on: the suppressed count
must match the result's. Suppressed rows are removed from ``Released.table``
(record suppression, Sweeney 2002); the all-rows form keeps its explicit name.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pyarrow as pa

from decoy_engine.anonymize.hierarchies import Lattice
from decoy_engine.anonymize.search._result import Released, SearchResult
from decoy_engine.quality.anonymity import FrequencySet


def _rows(table: pd.DataFrame | pa.Table) -> int:
    return len(table) if isinstance(table, pd.DataFrame) else table.num_rows


def apply_search(
    table: pd.DataFrame | pa.Table, lattice: Lattice, result: SearchResult
) -> Released:
    """``table`` generalized to ``result.node`` with the suppressed rows removed.

    The models come from ``result``. Raises ``ValueError`` when the result
    found no node or was computed on another lattice or table. Never modifies
    ``table``.
    """
    if not isinstance(table, (pd.DataFrame, pa.Table)):
        raise TypeError("table must be a pandas.DataFrame or a pyarrow.Table")
    if not isinstance(lattice, Lattice):
        raise TypeError("lattice must be an anonymize.hierarchies.Lattice")
    if not isinstance(result, SearchResult):
        raise TypeError("result must be a SearchResult")
    if not result.found or result.node is None:
        raise ValueError("the search found no node meeting the targets; nothing to apply")
    if lattice.columns != result.columns or lattice.heights != result.heights:
        raise ValueError("the result was computed on another lattice")
    if _rows(table) != result.rows:
        raise ValueError("the result was computed on another table (row count differs)")
    generalized = lattice.apply(table, result.node)
    frequencies = FrequencySet.of(generalized, lattice.columns, result.models)
    suppressed = frequencies.row_mask(frequencies.failing(result.models))
    if int(suppressed.sum()) != result.rows_suppressed:
        raise ValueError(
            "the result was computed on another table (suppressed rows differ when recomputed)"
        )
    keep = ~suppressed
    if isinstance(generalized, pd.DataFrame):
        released: pd.DataFrame | pa.Table = generalized[keep]
    else:
        released = generalized.filter(pa.array(keep, type=pa.bool_()))
    return Released(
        table=released,
        suppressed=np.asarray(suppressed, dtype=bool),
        generalized_including_suppressed=generalized,
    )

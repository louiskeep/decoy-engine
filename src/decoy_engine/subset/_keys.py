"""Key frames: the ONE key-column I/O boundary before materialization (SS5).

`load_key_frames` builds, per table, a polars DataFrame of the reserved
row-index column plus every key column any edge end or seed spec needs.
Projection pushdown (verified: `.select([key_cols]).collect()` on a
`scan_parquet` reads only those columns) keeps non-key columns unread until
`_materialize.py` re-touches the source files. Every downstream stage (seed
selection, closure, budget, estimate) operates ONLY on these frames.
"""

from __future__ import annotations

from collections.abc import Mapping

import polars as pl

from decoy_engine.subset._types import SeedSpec, SubsetEdge, SubsetSource

# Reserved row-index column name. Preflight fails (code subset_reserved_column)
# if any table already declares a column with this name.
RI = "__subset_ri"


def key_columns_needed(
    table: str, edges: tuple[SubsetEdge, ...], seeds: tuple[SeedSpec, ...]
) -> tuple[str, ...]:
    """Sorted, deduped union of every key column `table` needs loaded."""
    cols: set[str] = set()
    for edge in edges:
        if edge.parent_table == table:
            cols.update(edge.parent_columns)
        if edge.child_table == table:
            cols.update(edge.child_columns)
    for spec in seeds:
        if spec.table != table:
            continue
        cols.update(spec.key_columns)
        cols.update(p.column for p in spec.predicates)
    return tuple(sorted(cols))


def load_key_frames(
    sources: Mapping[str, SubsetSource],
    edges: tuple[SubsetEdge, ...],
    seeds: tuple[SeedSpec, ...],
) -> dict[str, pl.DataFrame]:
    """Load one key-only DataFrame per table (RI + needed key columns).

    Tables with no needed columns (isolated, unseeded) still get a frame of
    just RI so `input_rows` is known for the estimate.
    """
    frames: dict[str, pl.DataFrame] = {}
    for table in sorted(sources):
        cols = key_columns_needed(table, edges, seeds)
        lazy = pl.scan_parquet(sources[table].path).with_row_index(RI)
        frames[table] = lazy.select([RI, *cols]).collect()
    return frames

"""Arrow <-> engine conversion shims.

The Arrow-canonical runner cache (Phase 1 of the polars-duckdb hybrid plan)
holds `pyarrow.Table` between ops. Each op declares its `NATIVE_ENGINE` and
the runner materializes the cached Arrow into the op's preferred type at
`apply()` time, then converts the result back to Arrow before caching.

Engines:
    arrow   - pyarrow.Table; pass-through
    pandas  - pandas.DataFrame
    polars  - polars.DataFrame
    duckdb  - pyarrow.Table (DuckDB consumes Arrow natively via its
              relational API; ops register the table with their connection
              and execute SQL against it)

The polars / duckdb branches lazy-import so a pandas-only install (no `hybrid`
extra) doesn't pay an import cost or fail to load this module.
"""

from typing import Any, Literal

import pandas as pd
import pyarrow as pa

EngineType = Literal["pandas", "polars", "duckdb", "arrow"]

VALID_ENGINES: tuple[EngineType, ...] = ("pandas", "polars", "duckdb", "arrow")


def arrow_to_engine(table: pa.Table, engine: EngineType) -> Any:
    """Convert a pyarrow.Table to the op's native engine type.

    For 'arrow' / 'duckdb' this is a pass-through (DuckDB consumes Arrow
    natively). For 'pandas' / 'polars' we materialize.
    """
    if engine == "arrow" or engine == "duckdb":
        return table
    if engine == "pandas":
        # Zero-copy: types_mapper=pd.ArrowDtype wraps the Arrow buffer
        # rather than copying it. Measured at 5M rows: ~1100x faster than
        # the default to_pandas() (1.5s -> 1.4ms). All masking transforms
        # are backend-agnostic post-Bug 4 — see shuffle.py for the one
        # that needed the explicit fix.
        return table.to_pandas(types_mapper=pd.ArrowDtype)
    if engine == "polars":
        import polars as pl

        # rechunk=False asks Polars to reference Arrow's chunks instead of
        # copying every byte into a fresh contiguous buffer (its default).
        # For numeric columns this is fully zero-copy. For strings it
        # depends on Polars' internal layout vs Arrow's; if Polars can
        # share the buffer we save the dual-representation cost the Bug 5
        # calibration measured. If not, this is a no-op.
        return pl.from_arrow(table, rechunk=False)
    raise ValueError(f"unknown engine: {engine!r}")


def engine_to_arrow(result: Any, engine: EngineType) -> pa.Table:
    """Convert an op's output back to pyarrow.Table for caching."""
    if engine == "arrow":
        if not isinstance(result, pa.Table):
            raise TypeError(
                f"engine='arrow' op must return pyarrow.Table; got {type(result).__name__}"
            )
        return result
    if engine == "duckdb":
        # DuckDB ops return pyarrow.Table by convention (the .arrow() method
        # on a DuckDB relation). If a DuckDB op returns a relation directly,
        # convert here.
        if isinstance(result, pa.Table):
            return result
        # duckdb.DuckDBPyRelation has .arrow(); guard with hasattr to keep the
        # duckdb import lazy.
        if hasattr(result, "arrow"):
            return result.arrow()
        raise TypeError(
            f"engine='duckdb' op must return pyarrow.Table or DuckDBPyRelation; "
            f"got {type(result).__name__}"
        )
    if engine == "pandas":
        if not isinstance(result, pd.DataFrame):
            raise TypeError(
                f"engine='pandas' op must return pandas.DataFrame; got {type(result).__name__}"
            )
        return pa.Table.from_pandas(result, preserve_index=False)
    if engine == "polars":
        # polars.DataFrame.to_arrow() returns pa.Table; LazyFrame must be
        # collected first. We accept either by feature-detecting `.collect`.
        if hasattr(result, "collect") and not hasattr(result, "to_arrow"):
            result = result.collect()
        if hasattr(result, "to_arrow"):
            return result.to_arrow()
        raise TypeError(
            f"engine='polars' op must return polars.DataFrame; got {type(result).__name__}"
        )
    raise ValueError(f"unknown engine: {engine!r}")


def arrow_row_count(table: pa.Table) -> int:
    """Row count helper used by the runner for telemetry; 0 if None."""
    if table is None:
        return 0
    return table.num_rows


def arrow_columns(table: pa.Table) -> list[str]:
    """Column names; empty list if None."""
    if table is None:
        return []
    return list(table.column_names)

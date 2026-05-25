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

import logging
from typing import Any, Literal

import pandas as pd
import pyarrow as pa

EngineType = Literal["pandas", "polars", "duckdb", "arrow"]

VALID_ENGINES: tuple[EngineType, ...] = ("pandas", "polars", "duckdb", "arrow")

_logger = logging.getLogger(__name__)


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
        # DuckDB ops return pyarrow.Table by convention. If a DuckDB op
        # returns a relation directly, convert here via
        # to_arrow_table() -- the materialized form. rel.arrow() in
        # DuckDB 1.5.x returns a RecordBatchReader which leaks the open
        # connection and breaks downstream code that expects a Table.
        if isinstance(result, pa.Table):
            return result
        # duckdb.DuckDBPyRelation has to_arrow_table(); guard with
        # hasattr to keep the duckdb import lazy.
        if hasattr(result, "to_arrow_table"):
            return result.to_arrow_table()
        raise TypeError(
            f"engine='duckdb' op must return pyarrow.Table or DuckDBPyRelation; "
            f"got {type(result).__name__}"
        )
    if engine == "pandas":
        if not isinstance(result, pd.DataFrame):
            raise TypeError(
                f"engine='pandas' op must return pandas.DataFrame; got {type(result).__name__}"
            )
        # pyarrow's Table.from_pandas raises ValueError with the *full*
        # column list when names collide ("Duplicate column names found:
        # [<every column>]"), which is misleading on a wide frame. Pre-
        # check and raise a clearer error listing only the names that
        # actually appear more than once.
        cols = list(result.columns)
        if len(set(cols)) != len(cols):
            from collections import Counter

            dupes = sorted({name for name, n in Counter(cols).items() if n > 1})
            raise ValueError(
                f"Duplicate column names: {dupes}. Use drop_column upstream to remove the "
                "collision, or join's keyed mode with suffixes to disambiguate."
            )
        return _pandas_to_arrow_resilient(result)
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


def _pandas_to_arrow_resilient(df: pd.DataFrame) -> pa.Table:
    """``pa.Table.from_pandas`` wrapper that survives type-mismatch on
    pandas object columns.

    Why this exists: pandas object columns are typed permissively (any
    Python value), but pyarrow's ``Table.from_pandas`` chooses an Arrow
    type by sampling the first non-null value and then errors when a
    later value doesn't fit. The most common trigger is a CSV column
    that started as int64 (date-like values such as 20260522) but
    received string-valued masking output partway through, leaving
    pandas with ``dtype=object`` and a mix of ints + strings. The
    failure surfaces as the opaque tuple:

      ArrowTypeError(
        "object of type <class 'str'> cannot be converted to int",
        "Conversion failed for column ENRL_END_DT with type object",
      )

    We do NOT silently mask this with a re-cast on every conversion --
    that would mask real bugs. Instead: the happy path stays
    ``Table.from_pandas`` directly, with no per-column overhead. On
    failure we identify the offending column(s), coerce just those
    to string (the safest universal type), warn the operator via
    the standard engine logger, and retry. The string fallback
    preserves the visible value -- downstream nodes that expect a
    numeric type will fail at THAT boundary with a clearer message
    pointing at their type expectation rather than the deep pandas
    + arrow tuple here.

    Reference: https://arrow.apache.org/docs/python/pandas.html
    ("Object columns" section -- pyarrow infers from the first
    non-null value).
    """
    try:
        return pa.Table.from_pandas(df, preserve_index=False)
    except (pa.lib.ArrowTypeError, pa.lib.ArrowInvalid) as exc:
        # Identify the offending column(s) by trying each object column
        # independently. Non-object columns (already int64 / float64 /
        # datetime / extension-typed) skip the retry because they don't
        # hit this inference path.
        bad_cols: list[str] = []
        for col_name in df.columns:
            if df[col_name].dtype != object:
                continue
            try:
                pa.array(df[col_name])
            except (pa.lib.ArrowTypeError, pa.lib.ArrowInvalid):
                bad_cols.append(col_name)

        if not bad_cols:
            # The error wasn't about object-column inference; re-raise
            # the original so the caller sees the real cause instead of
            # a misleading "fixed nothing, retried, still broken".
            raise

        _logger.warning(
            "pandas->arrow conversion failed on object column(s) %s with "
            "mixed-type values; coercing to string. Original error: %s",
            bad_cols, exc,
        )

        # Coerce just the offending columns to all-string and retry. We
        # use .astype(str) on a copy so callers' DataFrames aren't
        # mutated. NaN/None values round-trip as 'nan'/'None' through
        # astype(str); use .where(notna, None) to keep nulls intact so
        # downstream null-handling stays correct.
        coerced = df.copy()
        for col_name in bad_cols:
            ser = coerced[col_name]
            coerced[col_name] = ser.where(ser.notna(), None).astype(object).map(
                lambda v: None if v is None else str(v),
            )
        return pa.Table.from_pandas(coerced, preserve_index=False)


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

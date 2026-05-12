"""source.file — read a CSV/parquet file into a DataFrame.

Config:
    path: str            - filesystem path
    format: 'csv' | 'parquet'  (optional; inferred from extension)

Phase 4 of the polars-duckdb hybrid plan: NATIVE_ENGINE='duckdb'. The
DuckDB path streams CSVs natively (no need for the dead chunked-CSV
iterator the cheap-wins memo planned to wire) and uses query optimizer
pushdown for parquet column projection. Pandas fallback retained for
graph engine mode = pandas.
"""

from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa

from decoy_engine.graph.ops._base import OpError
from decoy_engine.internal.validator import ValidationError

KIND = "source.file"
NATIVE_ENGINE = "duckdb"
INPUT_ARITY: tuple[int, int | None] = (0, 0)
OUTPUT_KIND = "stream"


def validate_config(config: dict[str, Any]) -> None:
    if "path" not in config:
        raise ValidationError("missing required field 'path'", "config.path")
    fmt = (config.get("format") or _infer_format(config["path"])).lower()
    if fmt not in {"csv", "parquet"}:
        raise ValidationError(
            f"unsupported format {fmt!r} (csv|parquet)", "config.format"
        )


def apply(inputs, config, ctx):
    engine = config.get("__engine", "pandas")
    path = Path(config["path"])
    fmt = (config.get("format") or _infer_format(str(path))).lower()
    if engine == "duckdb":
        result = _apply_duckdb(config)
        row_count = result.num_rows
        column_count = len(result.column_names)
    else:
        result = _apply_pandas(config)
        row_count = len(result)
        column_count = len(result.columns)
    if ctx is not None and hasattr(ctx, "export"):
        ctx.export("row_count", int(row_count))
        ctx.export("column_count", int(column_count))
        ctx.export("inferred_format", fmt)
        try:
            ctx.export("file_size_bytes", int(path.stat().st_size))
        except OSError:
            # File might be a stream / FUSE mount that doesn't stat cleanly.
            # Skip rather than fail the op for a metric.
            pass
    return result


def _apply_pandas(config: dict[str, Any]) -> pd.DataFrame:
    path = Path(config["path"])
    fmt = (config.get("format") or _infer_format(str(path))).lower()
    row_limit = config.get("__preview_row_limit")
    try:
        if fmt == "csv":
            return pd.read_csv(path, nrows=row_limit)
        if fmt == "parquet":
            df = pd.read_parquet(path)
            return df.head(row_limit) if row_limit else df
    except Exception as exc:
        raise OpError(f"failed to read {path}: {exc}") from exc
    raise OpError(f"unsupported format: {fmt}")


def _apply_duckdb(config: dict[str, Any]) -> pa.Table:
    import duckdb

    path = Path(config["path"])
    fmt = (config.get("format") or _infer_format(str(path))).lower()
    row_limit = config.get("__preview_row_limit")

    try:
        # In-memory DuckDB connection per op: cheap, isolated, GC'd at the
        # end of apply(). The relation is materialized to Arrow before the
        # connection closes.
        con = duckdb.connect(":memory:")
        try:
            if fmt == "csv":
                # read_csv_auto handles header / dtype inference. LIMIT
                # pushed into the query so DuckDB only reads what we need.
                sql = f"SELECT * FROM read_csv_auto('{path}')"
            elif fmt == "parquet":
                sql = f"SELECT * FROM read_parquet('{path}')"
            else:
                raise OpError(f"unsupported format: {fmt}")
            if row_limit:
                sql += f" LIMIT {int(row_limit)}"
            # to_arrow_table returns pa.Table; .arrow() returns a
            # RecordBatchReader which isn't what the runner cache wants.
            return con.execute(sql).to_arrow_table()
        finally:
            con.close()
    except OpError:
        raise
    except Exception as exc:
        raise OpError(f"failed to read {path}: {exc}") from exc


def _infer_format(path: str) -> str:
    suffix = Path(path).suffix.lower().lstrip(".")
    if suffix in {"parquet", "pq"}:
        return "parquet"
    return "csv"

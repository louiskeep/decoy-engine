"""source.file — read a CSV/parquet file into a DataFrame.

Config:
    path: str            - filesystem path
    format: 'csv' | 'parquet'  (optional; inferred from extension)
    has_header: bool     - CSV only; defaults to true. When false, column
                           names are auto-generated (col_0, col_1, ...) and
                           the first row is treated as data.

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
    if "has_header" in config and not isinstance(config["has_header"], bool):
        raise ValidationError(
            "'has_header' must be a boolean when set", "config.has_header"
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
    has_header = config.get("has_header", True)
    try:
        if fmt == "csv":
            if has_header:
                return pd.read_csv(path, nrows=row_limit)
            # header=None tells pandas there's no header row; column labels
            # default to RangeIndex which serializes as 0,1,2,... We rename
            # to col_0, col_1, ... so downstream node configs that reference
            # column names by string survive (numeric labels stringify in
            # surprising ways when serialized to YAML / CSV).
            df = pd.read_csv(path, nrows=row_limit, header=None)
            df.columns = [f"col_{i}" for i in range(len(df.columns))]
            return df
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

    has_header = config.get("has_header", True)
    try:
        # In-memory DuckDB connection per op: cheap, isolated, GC'd at the
        # end of apply(). The relation is materialized to Arrow before the
        # connection closes.
        con = duckdb.connect(":memory:")
        try:
            if fmt == "csv":
                # read_csv_auto handles header / dtype inference. LIMIT
                # pushed into the query so DuckDB only reads what we need.
                # When has_header=false, DuckDB auto-generates column0..N
                # which we rename to col_0..N for parity with the pandas
                # path (string column names downstream).
                header_arg = "true" if has_header else "false"
                sql = f"SELECT * FROM read_csv_auto('{path}', header={header_arg})"
            elif fmt == "parquet":
                sql = f"SELECT * FROM read_parquet('{path}')"
            else:
                raise OpError(f"unsupported format: {fmt}")
            if row_limit:
                sql += f" LIMIT {int(row_limit)}"
            # to_arrow_table returns pa.Table; .arrow() returns a
            # RecordBatchReader which isn't what the runner cache wants.
            table = con.execute(sql).to_arrow_table()
            if fmt == "csv" and not has_header:
                # Normalize DuckDB's `column0`/`column1` to `col_0`/`col_1`
                # so downstream configs reference the same names regardless
                # of which substrate read the file.
                table = table.rename_columns(
                    [f"col_{i}" for i in range(table.num_columns)]
                )
            return table
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

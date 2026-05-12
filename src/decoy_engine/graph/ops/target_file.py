"""target.file — write a DataFrame to CSV/parquet.

Config:
    output_filename: str  - filesystem path to write
    format: 'csv' | 'parquet'  (optional; inferred from extension)

Phase 4 port: NATIVE_ENGINE='duckdb'. The DuckDB path uses `COPY ... TO`
which streams the write and produces well-formed parquet without going
through pandas. Pandas fallback retained for graph engine mode = pandas.
"""

from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa

from decoy_engine.graph.ops._base import OpError
from decoy_engine.internal.validator import ValidationError

KIND = "target.file"
NATIVE_ENGINE = "duckdb"
INPUT_ARITY: tuple[int, int | None] = (1, 1)
OUTPUT_KIND = "sink"


def validate_config(config: dict[str, Any]) -> None:
    if "output_filename" not in config:
        raise ValidationError(
            "missing required field 'output_filename'", "config.output_filename"
        )
    fmt = (config.get("format") or _infer_format(config["output_filename"])).lower()
    if fmt not in {"csv", "parquet"}:
        raise ValidationError(
            f"unsupported format {fmt!r} (csv|parquet)", "config.format"
        )


def apply(inputs, config, ctx):
    df = inputs[0]
    if config.get("__preview_row_limit") is not None:
        # Preview mode: don't actually write — return what would be written.
        return df

    engine = config.get("__engine", "pandas")
    # Row count is known from the input shape regardless of engine path —
    # capture before delegating so both pandas and duckdb branches share
    # the same export semantics.
    if engine == "duckdb":
        rows_written = int(df.num_rows)
        result = _apply_duckdb(df, config)
    else:
        rows_written = int(len(df))
        result = _apply_pandas(df, config)
    if ctx is not None and hasattr(ctx, "export"):
        path = Path(config["output_filename"])
        ctx.export("rows_written", rows_written)
        ctx.export("output_path", str(path.resolve()))
        try:
            ctx.export("output_file_size_bytes", int(path.stat().st_size))
        except OSError:
            pass
    return result


def _apply_pandas(df: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    path = Path(config["output_filename"])
    path.parent.mkdir(parents=True, exist_ok=True)
    fmt = (config.get("format") or _infer_format(str(path))).lower()
    try:
        if fmt == "csv":
            df.to_csv(path, index=False)
        elif fmt == "parquet":
            df.to_parquet(path, index=False)
    except Exception as exc:
        raise OpError(f"failed to write {path}: {exc}") from exc
    return df.head(0)


def _apply_duckdb(table: pa.Table, config: dict[str, Any]) -> pa.Table:
    import duckdb

    path = Path(config["output_filename"])
    path.parent.mkdir(parents=True, exist_ok=True)
    fmt = (config.get("format") or _infer_format(str(path))).lower()

    try:
        con = duckdb.connect(":memory:")
        try:
            # Register the in-memory Arrow table with DuckDB and let COPY
            # stream it to disk. This is faster and uses less RAM than
            # `df.to_parquet` for big tables because COPY writes in batches.
            con.register("in_table", table)
            if fmt == "csv":
                con.execute(
                    f"COPY (SELECT * FROM in_table) TO '{path}' "
                    f"(FORMAT CSV, HEADER)"
                )
            elif fmt == "parquet":
                con.execute(
                    f"COPY (SELECT * FROM in_table) TO '{path}' "
                    f"(FORMAT PARQUET)"
                )
            else:
                raise OpError(f"unsupported format: {fmt}")
        finally:
            con.close()
    except OpError:
        raise
    except Exception as exc:
        raise OpError(f"failed to write {path}: {exc}") from exc

    # Sinks return an empty value of the same type as their input so the
    # runner's engine_to_arrow shim is happy. For duckdb that's an empty
    # Arrow table.
    return table.slice(0, 0)


def _infer_format(path: str) -> str:
    suffix = Path(path).suffix.lower().lstrip(".")
    if suffix in {"parquet", "pq"}:
        return "parquet"
    return "csv"

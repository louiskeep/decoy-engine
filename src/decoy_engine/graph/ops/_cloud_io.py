"""Shared download/upload helpers for cloud file source/sink graph ops.

All three cloud source ops (source.s3, source.gcs, source.sftp) follow
the same read pattern:
  1. Open a streaming connection via the SDK FileSource.open().
  2. Write chunks to a local temp file.
  3. Hand the temp path to DuckDB (or pandas) for format-aware reading.
  4. Delete the temp file.

target.s3 / target.gcs / target.sftp invert the flow:
  1. Write the DataFrame / Arrow table to a temp file via DuckDB / pandas.
  2. Stream the temp file bytes through FileSink.write().
  3. Delete the temp file.

Centralising this avoids duplicating temp-file lifecycle and
format-inference code across six op modules.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Iterator

import pandas as pd
import pyarrow as pa

from decoy_engine.graph.ops._base import OpError
from decoy_engine.internal.validator import ValidationError


def infer_format(path: str) -> str:
    """Infer csv/parquet from file extension; default to csv."""
    suffix = Path(path).suffix.lower().lstrip(".")
    if suffix in {"parquet", "pq"}:
        return "parquet"
    return "csv"


def validate_format(fmt: str) -> None:
    if fmt not in {"csv", "parquet"}:
        raise ValidationError(
            f"unsupported format {fmt!r} (csv|parquet)", "config.format"
        )


# ----- local file readers -----------------------------------------------


def _read_duckdb(tmp_path: Path, fmt: str, row_limit) -> pa.Table:
    import duckdb

    try:
        con = duckdb.connect(":memory:")
        try:
            if fmt == "csv":
                sql = f"SELECT * FROM read_csv_auto('{tmp_path}')"
            elif fmt == "parquet":
                sql = f"SELECT * FROM read_parquet('{tmp_path}')"
            else:
                raise OpError(f"unsupported format: {fmt}")
            if row_limit:
                sql += f" LIMIT {int(row_limit)}"
            return con.execute(sql).to_arrow_table()
        finally:
            con.close()
    except OpError:
        raise
    except Exception as exc:
        raise OpError(f"failed to read downloaded file: {exc}") from exc


def _read_pandas(tmp_path: Path, fmt: str, row_limit) -> pd.DataFrame:
    try:
        if fmt == "csv":
            return pd.read_csv(tmp_path, nrows=row_limit)
        if fmt == "parquet":
            df = pd.read_parquet(tmp_path)
            return df.head(row_limit) if row_limit else df
    except Exception as exc:
        raise OpError(f"failed to read downloaded file: {exc}") from exc
    raise OpError(f"unsupported format: {fmt}")


# ----- local file writers -----------------------------------------------


def _write_duckdb(table: pa.Table, tmp_path: Path, fmt: str) -> None:
    import duckdb

    try:
        con = duckdb.connect(":memory:")
        try:
            con.register("in_table", table)
            if fmt == "csv":
                con.execute(
                    f"COPY (SELECT * FROM in_table) TO '{tmp_path}' "
                    f"(FORMAT CSV, HEADER)"
                )
            elif fmt == "parquet":
                con.execute(
                    f"COPY (SELECT * FROM in_table) TO '{tmp_path}' "
                    f"(FORMAT PARQUET)"
                )
            else:
                raise OpError(f"unsupported format: {fmt}")
        finally:
            con.close()
    except OpError:
        raise
    except Exception as exc:
        raise OpError(f"failed to write temp file: {exc}") from exc


def _write_pandas(df: pd.DataFrame, tmp_path: Path, fmt: str) -> None:
    try:
        if fmt == "csv":
            df.to_csv(tmp_path, index=False)
        elif fmt == "parquet":
            df.to_parquet(tmp_path, index=False)
        else:
            raise OpError(f"unsupported format: {fmt}")
    except OpError:
        raise
    except Exception as exc:
        raise OpError(f"failed to write temp file: {exc}") from exc


def _iter_file(path: Path, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                return
            yield chunk


# ----- public helpers ---------------------------------------------------


def download_and_read(source, remote_path: str, config: dict[str, Any]) -> Any:
    """Stream remote_path from source into a temp file; read into DataFrame/Table.

    Returns pa.Table when engine='duckdb', pd.DataFrame when 'pandas'.
    The temp file is deleted on exit regardless of success or failure.
    """
    fmt = (config.get("format") or infer_format(remote_path)).lower()
    engine = config.get("__engine", "pandas")
    row_limit = config.get("__preview_row_limit")

    suffix = ".parquet" if fmt == "parquet" else ".csv"
    fd, tmp_str = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    tmp_path = Path(tmp_str)
    try:
        with tmp_path.open("wb") as f:
            for chunk in source.open(remote_path):
                f.write(chunk)
        if engine == "duckdb":
            return _read_duckdb(tmp_path, fmt, row_limit)
        return _read_pandas(tmp_path, fmt, row_limit)
    finally:
        tmp_path.unlink(missing_ok=True)


def write_and_upload(df_or_table, sink, remote_path: str, config: dict[str, Any]) -> Any:
    """Write DataFrame/Table to a temp file; stream it to sink.write().

    Returns a zero-row stub of the same type to match the target.file
    convention (the runner's post-op handling expects this shape).
    """
    fmt = (config.get("format") or infer_format(remote_path)).lower()
    engine = config.get("__engine", "pandas")

    suffix = ".parquet" if fmt == "parquet" else ".csv"
    fd, tmp_str = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    tmp_path = Path(tmp_str)
    try:
        if engine == "duckdb":
            _write_duckdb(df_or_table, tmp_path, fmt)
        else:
            _write_pandas(df_or_table, tmp_path, fmt)
        sink.write(remote_path, _iter_file(tmp_path))
    finally:
        tmp_path.unlink(missing_ok=True)

    if isinstance(df_or_table, pa.Table):
        return df_or_table.slice(0, 0)
    return df_or_table.head(0)

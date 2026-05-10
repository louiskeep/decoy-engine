"""target.db — write a DataFrame to a SQL database.

Config:
    table: str             - destination table name (required)
    schema: str            - optional
    write_mode: 'append' | 'replace' | 'fail'  - default 'append'
    dsn: str               - direct DSN (CLI)
    connector_id: int      - platform path; resolved via ctx.resolve_connector

Phase 4 port: NATIVE_ENGINE='duckdb'. The DuckDB path materializes the
input Arrow table to pandas at the SQLAlchemy boundary (to_sql is the
universal sink across dialects). DuckDB's INSERT ... SELECT against an
attached target is faster but requires both ends in DuckDB — that's a
follow-up enhancement.
"""

from typing import Any

import pandas as pd
import pyarrow as pa

from decoy_engine.graph.ops._base import OpError
from decoy_engine.internal.validator import ValidationError

KIND = "target.db"
NATIVE_ENGINE = "duckdb"
INPUT_ARITY: tuple[int, int | None] = (1, 1)
OUTPUT_KIND = "sink"

_VALID_WRITE_MODES = {"append", "replace", "fail"}


def validate_config(config: dict[str, Any]) -> None:
    if "table" not in config:
        raise ValidationError("missing required field 'table'", "config.table")
    if not config.get("dsn") and config.get("connector_id") is None:
        raise ValidationError(
            "must provide either 'dsn' or 'connector_id'", "config"
        )
    mode = config.get("write_mode", "append")
    if mode not in _VALID_WRITE_MODES:
        raise ValidationError(
            f"'write_mode' must be one of {sorted(_VALID_WRITE_MODES)}",
            "config.write_mode",
        )


def apply(inputs, config, ctx):
    df = inputs[0]
    if config.get("__preview_row_limit") is not None:
        # Preview mode: don't write — just return the data we'd have written.
        return df

    engine = config.get("__engine", "pandas")
    if engine == "duckdb":
        return _apply_duckdb(df, config, ctx)
    return _apply_pandas(df, config, ctx)


def _apply_pandas(df: pd.DataFrame, config: dict[str, Any], ctx) -> pd.DataFrame:
    dsn = _resolve_dsn(config, ctx)
    table = config["table"]
    schema = config.get("schema") or None
    mode = config.get("write_mode", "append")

    try:
        from sqlalchemy import create_engine

        engine = create_engine(dsn)
        try:
            df.to_sql(table, engine, schema=schema, if_exists=mode, index=False)
        finally:
            engine.dispose()
    except Exception as exc:
        raise OpError(f"target.db write failed: {exc}") from exc

    return df.head(0)


def _apply_duckdb(table: pa.Table, config: dict[str, Any], ctx) -> pa.Table:
    dsn = _resolve_dsn(config, ctx)
    target_table = config["table"]
    schema = config.get("schema") or None
    mode = config.get("write_mode", "append")

    try:
        from sqlalchemy import create_engine

        # to_sql universally works across SQLAlchemy dialects; the Arrow →
        # pandas conversion here is the cost we pay for using SQLAlchemy.
        # For DuckDB-on-DuckDB writes, INSERT ... SELECT is faster — gated
        # behind dsn scheme as a follow-up.
        df = table.to_pandas()
        engine = create_engine(dsn)
        try:
            df.to_sql(
                target_table, engine, schema=schema, if_exists=mode, index=False
            )
        finally:
            engine.dispose()
    except Exception as exc:
        raise OpError(f"target.db write failed: {exc}") from exc

    return table.slice(0, 0)


def _resolve_dsn(config: dict[str, Any], ctx) -> str:
    if config.get("dsn"):
        return config["dsn"]
    if ctx is None or getattr(ctx, "resolve_connector", None) is None:
        raise OpError(
            "connector_id provided but ctx.resolve_connector is None "
            "(CLI must use inline 'dsn' instead)"
        )
    return ctx.resolve_connector(int(config["connector_id"]))

"""target.db -- write a DataFrame to a SQL database.

Config:
    table: str             - destination table name (required)
    schema: str            - optional
    write_mode: 'append' | 'replace' | 'fail'  - default 'append'
    dsn: str               - direct DSN (CLI)
    connector_id: int      - platform path; resolved via ctx.resolve_connector

NATIVE_ENGINE='duckdb'. Symmetric with `source.db`: dispatches on DSN
dialect.

- **SQLite** -> DuckDB `sqlite_scanner` extension via `ATTACH (TYPE sqlite)`,
  then INSERT / CREATE TABLE AS SELECT against the attached target.
  The Arrow input is registered as a view so the write doesn't go
  through pandas.
- **Postgres** -> DuckDB `postgres_scanner` via `ATTACH (TYPE postgres)`.
  Same shape; needs a running Postgres for tests.
- **Everything else** -> SQLAlchemy + `df.to_sql()` fallback. Pays the
  Arrow -> pandas conversion cost.

See `source_db.py` for the dispatch helpers; this op shares them.

SECURITY (Sprint 6): table and schema identifiers are validated by
_validate_sql_identifier() before entering query strings, preventing
double-quote injection and semicolons in identifier values.
"""

from typing import Any

import pandas as pd
import pyarrow as pa

from decoy_engine.graph.ops._base import OpError
from decoy_engine.graph.ops.source_db import (
    _attach_target_for,
    _resolve_scanner,
    _validate_sql_identifier,
)
from decoy_engine.internal.validator import ValidationError

KIND = "target.db"
NATIVE_ENGINE = "duckdb"
INPUT_ARITY: tuple[int, int | None] = (1, 1)
OUTPUT_KIND = "sink"

_VALID_WRITE_MODES = {"append", "replace", "fail"}


def validate_config(config: dict[str, Any]) -> None:
    if "table" not in config:
        raise ValidationError("missing required field 'table'", "config.table")
    _validate_sql_identifier(config["table"], "config.table")
    if config.get("schema"):
        _validate_sql_identifier(config["schema"], "config.schema")
    if not config.get("dsn") and config.get("connector_id") is None:
        raise ValidationError("must provide either 'dsn' or 'connector_id'", "config")
    mode = config.get("write_mode", "append")
    if mode not in _VALID_WRITE_MODES:
        raise ValidationError(
            f"'write_mode' must be one of {sorted(_VALID_WRITE_MODES)}",
            "config.write_mode",
        )


def apply(inputs, config, ctx):
    df = inputs[0]
    if config.get("__preview_row_limit") is not None:
        # Preview mode: don't write -- just return the data we'd have written.
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
    scanner = _resolve_scanner(dsn)
    if scanner is not None:
        _apply_duckdb_native_scanner(scanner, dsn, table, config)
    else:
        _apply_duckdb_sqlalchemy_fallback(dsn, table, config)
    # Sinks return an empty slice by convention.
    return table.slice(0, 0)


def _apply_duckdb_native_scanner(
    scanner: tuple[str, str],
    dsn: str,
    arrow_table: pa.Table,
    config: dict[str, Any],
) -> None:
    """DuckDB writes directly to the attached database; the Arrow input
    is registered as a view so the write skips the pandas materialization.
    """
    extension, attach_type = scanner
    attach_target = _attach_target_for(dsn, attach_type)

    target_table = config["table"]
    schema = config.get("schema") or None
    mode = config.get("write_mode", "append")

    qualified = f'dst."{schema}"."{target_table}"' if schema else f'dst."{target_table}"'

    try:
        import duckdb

        con = duckdb.connect()
        try:
            con.execute(f"INSTALL {extension}")
            con.execute(f"LOAD {extension}")
            con.execute(f"ATTACH '{attach_target}' AS dst (TYPE {attach_type})")
            con.register("input_data", arrow_table)

            if mode == "replace":
                con.execute(f"DROP TABLE IF EXISTS {qualified}")
                con.execute(f"CREATE TABLE {qualified} AS SELECT * FROM input_data")
            elif mode == "fail":
                con.execute(f"CREATE TABLE {qualified} AS SELECT * FROM input_data")
            else:  # append
                con.execute(
                    f"CREATE TABLE IF NOT EXISTS {qualified} AS SELECT * FROM input_data WHERE 0=1"
                )
                con.execute(f"INSERT INTO {qualified} SELECT * FROM input_data")
        finally:
            con.close()
    except Exception as exc:
        raise OpError(f"target.db write failed: {exc}") from exc


def _apply_duckdb_sqlalchemy_fallback(
    dsn: str, arrow_table: pa.Table, config: dict[str, Any]
) -> None:
    """SQLAlchemy + pandas.to_sql fallback for DBs without a DuckDB
    scanner extension."""
    target_table = config["table"]
    schema = config.get("schema") or None
    mode = config.get("write_mode", "append")

    try:
        from sqlalchemy import create_engine

        df = arrow_table.to_pandas()
        engine = create_engine(dsn)
        try:
            df.to_sql(target_table, engine, schema=schema, if_exists=mode, index=False)
        finally:
            engine.dispose()
    except Exception as exc:
        raise OpError(f"target.db write failed: {exc}") from exc


def _resolve_dsn(config: dict[str, Any], ctx) -> str:
    if config.get("dsn"):
        return config["dsn"]
    if ctx is None or getattr(ctx, "resolve_connector", None) is None:
        raise OpError(
            "connector_id provided but ctx.resolve_connector is None "
            "(CLI must use inline 'dsn' instead)"
        )
    return ctx.resolve_connector(int(config["connector_id"]))

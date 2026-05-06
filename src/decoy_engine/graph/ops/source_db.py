"""source.db — read a table from a SQL database into a DataFrame.

Config:
    table: str             - table name (required)
    schema: str            - optional, db-specific
    where: str             - optional SQL WHERE clause (no leading "WHERE")
    dsn: str               - direct SQLAlchemy DSN (CLI path)
    connector_id: int      - platform path; resolved via ctx.resolve_connector

Exactly one of `dsn` or `connector_id` must be present. When both are
provided, `dsn` wins.
"""

from typing import Any

import pandas as pd

from decoy_engine.graph.ops._base import OpError
from decoy_engine.internal.validator import ValidationError

KIND = "source.db"
INPUT_ARITY: tuple[int, int | None] = (0, 0)
OUTPUT_KIND = "stream"


def validate_config(config: dict[str, Any]) -> None:
    if "table" not in config:
        raise ValidationError("missing required field 'table'", "config.table")
    if not config.get("dsn") and config.get("connector_id") is None:
        raise ValidationError(
            "must provide either 'dsn' or 'connector_id'", "config"
        )


def apply(inputs, config, ctx) -> pd.DataFrame:
    dsn = _resolve_dsn(config, ctx)
    table = config["table"]
    schema = config.get("schema") or None
    where = config.get("where")
    row_limit = config.get("__preview_row_limit")

    qualified = f'"{schema}"."{table}"' if schema else f'"{table}"'
    sql = f"SELECT * FROM {qualified}"
    if where:
        sql += f" WHERE {where}"
    if row_limit:
        sql += f" LIMIT {int(row_limit)}"

    try:
        from sqlalchemy import create_engine

        engine = create_engine(dsn)
        try:
            return pd.read_sql(sql, engine)
        finally:
            engine.dispose()
    except Exception as exc:
        raise OpError(f"source.db read failed: {exc}") from exc


def _resolve_dsn(config: dict[str, Any], ctx) -> str:
    if config.get("dsn"):
        return config["dsn"]
    if ctx is None or getattr(ctx, "resolve_connector", None) is None:
        raise OpError(
            "connector_id provided but ctx.resolve_connector is None "
            "(CLI must use inline 'dsn' instead)"
        )
    return ctx.resolve_connector(int(config["connector_id"]))

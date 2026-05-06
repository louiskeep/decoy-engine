"""target.db — write a DataFrame to a SQL database.

Config:
    table: str             - destination table name (required)
    schema: str            - optional
    write_mode: 'append' | 'replace' | 'fail'  - default 'append'
    dsn: str               - direct DSN (CLI)
    connector_id: int      - platform path; resolved via ctx.resolve_connector
"""

from typing import Any

import pandas as pd

from decoy_engine.graph.ops._base import OpError
from decoy_engine.internal.validator import ValidationError

KIND = "target.db"
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


def apply(inputs, config, ctx) -> pd.DataFrame:
    df = inputs[0]
    if config.get("__preview_row_limit") is not None:
        # Preview mode: don't write — just return the data we'd have written.
        return df

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


def _resolve_dsn(config: dict[str, Any], ctx) -> str:
    if config.get("dsn"):
        return config["dsn"]
    if ctx is None or getattr(ctx, "resolve_connector", None) is None:
        raise OpError(
            "connector_id provided but ctx.resolve_connector is None "
            "(CLI must use inline 'dsn' instead)"
        )
    return ctx.resolve_connector(int(config["connector_id"]))

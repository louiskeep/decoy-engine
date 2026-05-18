"""derive -- add a computed column from an arithmetic expression.

Config:
    column: str       - name of the new column (required)
    expression: str   - expression over existing columns

Note: the per-column `formula` strategy (inside Mask) is row-major and runs
inside the masker. `derive` is a graph node that operates on the whole frame
at once and lives outside Mask/Generate, so a YAML can compute a column
without configuring a Mask node just for that one job.

Phase 3 of the polars-duckdb hybrid plan: NATIVE_ENGINE='polars'. The polars
implementation uses `pl.SQLContext` to evaluate the expression -- pandas-eval
syntax for arithmetic / boolean is a subset of SQL expressions for the cases
we actually use (`a + b`, `price * 1.1`, `discount > 0`).

SECURITY: 'expression' is user YAML config concatenated into a Polars
SQLContext SQL string (in-memory only, no external DB). Medium risk --
see docs/security/sql-surfaces.md. Fix planned for Sprint 6.
"""

from typing import Any

import pandas as pd

from decoy_engine.graph.ops._base import OpError, is_polars_frame
from decoy_engine.internal.validator import ValidationError

KIND = "derive"
NATIVE_ENGINE = "polars"
INPUT_ARITY: tuple[int, int | None] = (1, 1)
OUTPUT_KIND = "stream"


def validate_config(config: dict[str, Any]) -> None:
    column = config.get("column")
    if not isinstance(column, str) or not column.strip():
        raise ValidationError("'column' must be a non-empty string", "config.column")

    expression = config.get("expression")
    if not isinstance(expression, str) or not expression.strip():
        raise ValidationError(
            "'expression' must be a non-empty string", "config.expression"
        )


def apply(inputs, config, ctx):
    df = inputs[0]
    column = config["column"]
    expression = config["expression"]
    if is_polars_frame(df):
        return _apply_polars(df, column, expression)
    return _apply_pandas(df, column, expression)


def _apply_pandas(df: pd.DataFrame, column: str, expression: str) -> pd.DataFrame:
    out = df.copy()
    try:
        out[column] = df.eval(expression, engine="python")
    except Exception as exc:
        raise OpError(
            f"derive expression failed for column {column!r} ({expression!r}): {exc}"
        ) from exc
    return out


def _apply_polars(df, column: str, expression: str):
    import polars as pl

    try:
        # S608: expression is user YAML config concatenated into SQL.
        # In-memory Polars SQLContext -- no external DB. Medium risk.
        # See docs/security/sql-surfaces.md. Fix planned for Sprint 6.
        # SELECT * preserves all original columns; the computed column is
        # appended via a SELECT alias. SQLContext quotes the alias so
        # column names with spaces work.
        sql = f'SELECT *, ({expression}) AS "{column}" FROM df'  # noqa: S608
        with pl.SQLContext(df=df, eager=True) as ctx:
            return ctx.execute(sql)
    except Exception as exc:
        raise OpError(
            f"derive expression failed for column {column!r} ({expression!r}): {exc}"
        ) from exc

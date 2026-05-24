"""derive -- add a computed column from an arithmetic expression.

Config:
    column: str       - name of the new column (required)
    expression: str   - expression over existing columns

Note: the per-column `formula` strategy (inside Mask) is row-major and runs
inside the masker. `derive` is a graph node that operates on the whole frame
at once and lives outside Mask/Generate, so a YAML can compute a column
without configuring a Mask node just for that one job.

Phase 3 of the polars-duckdb hybrid plan: NATIVE_ENGINE='polars'. The polars
implementation uses pl.sql_expr() to parse the expression into a Polars Expr
and applies it with df.with_columns(expr.alias(column)). No SQL string is
ever constructed -- the expression is evaluated against the frame's columns
only.

Sprint 6: replaced pl.SQLContext SQL string construction with pl.sql_expr()
+ df.with_columns(). See docs/security/sql-surfaces.md for the S608 history.
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
        raise ValidationError("'expression' must be a non-empty string", "config.expression")


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
    """Add or replace `column` with the result of `expression`.

    pl.sql_expr() parses the expression into a Polars Expr; with_columns
    evaluates it against the original frame before writing the output column,
    so overwriting an existing column name works correctly.
    """
    import polars as pl

    try:
        return df.with_columns(pl.sql_expr(expression).alias(column))
    except Exception as exc:
        raise OpError(
            f"derive expression failed for column {column!r} ({expression!r}): {exc}"
        ) from exc

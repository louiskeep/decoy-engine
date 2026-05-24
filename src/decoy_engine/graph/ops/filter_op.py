"""filter -- keep rows that match a predicate.

Config:
    predicate: str   - e.g. "state = 'CA' and age >= 18"

Per Q2 in PIPELINE_GRAPH_GUIDE.md, MVP shipped pandas df.query() syntax.

Phase 3 of the polars-duckdb hybrid plan: NATIVE_ENGINE='polars'. The polars
implementation uses pl.sql_expr() to parse the predicate into a Polars Expr
and applies it with df.filter(). This avoids SQL string construction: the
predicate is parsed as an expression tree evaluated against the in-memory
frame's columns only -- no external table access, no UNION/subquery surface.

Sprint 6: replaced pl.SQLContext SQL string construction with pl.sql_expr().
See docs/security/sql-surfaces.md for the S608 surface history.
"""

from typing import Any

import pandas as pd

from decoy_engine.errors import ValidationError
from decoy_engine.graph.ops._base import OpError, is_polars_frame

KIND = "filter"
NATIVE_ENGINE = "polars"
INPUT_ARITY: tuple[int, int | None] = (1, 1)
OUTPUT_KIND = "stream"


def validate_config(config: dict[str, Any]) -> None:
    pred = config.get("predicate")
    if not isinstance(pred, str) or not pred.strip():
        raise ValidationError("'predicate' must be a non-empty string", "config.predicate")


def apply(inputs, config, ctx):
    df = inputs[0]
    predicate = config["predicate"]
    rows_in = _frame_len(df)
    if is_polars_frame(df):
        result = _apply_polars(df, predicate)
    else:
        result = _apply_pandas(df, predicate)
    if ctx is not None and hasattr(ctx, "export"):
        rows_out = _frame_len(result)
        ctx.export("rows_in", rows_in)
        ctx.export("rows_out", rows_out)
        ctx.export("selectivity", (rows_out / rows_in) if rows_in else 0.0)
    return result


def _frame_len(frame: Any) -> int:
    """Length helper that works for both pandas and polars frames."""
    return len(frame)


def _apply_pandas(df: pd.DataFrame, predicate: str) -> pd.DataFrame:
    try:
        return df.query(predicate, engine="python")
    except Exception as exc:
        raise OpError(f"filter predicate failed ({predicate!r}): {exc}") from exc


def _apply_polars(df, predicate: str):
    """Evaluate the predicate using the Polars expression API.

    pl.sql_expr() parses a SQL expression string into a Polars Expr object.
    Evaluation is scoped to the frame's own columns -- no SQL string is ever
    constructed, so UNION / stacked-query injection is not possible.
    """
    import polars as pl

    try:
        return df.filter(pl.sql_expr(predicate))
    except Exception as exc:
        raise OpError(f"filter predicate failed ({predicate!r}): {exc}") from exc

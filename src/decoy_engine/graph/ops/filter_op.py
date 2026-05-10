"""filter — keep rows that match a predicate.

Config:
    predicate: str   - e.g. "state == 'CA' and age >= 18"

Per Q2 in PIPELINE_GRAPH_GUIDE.md, MVP shipped pandas df.query() syntax.

Phase 3 of the polars-duckdb hybrid plan: NATIVE_ENGINE='polars'. The polars
implementation uses `pl.SQLContext` which accepts the same boolean-expression
shape pandas-query supports for our usage (==, !=, <, >, <=, >=, and, or,
not, parentheses, single-quoted string literals). Documented divergences are
captured in tests/parity/SEMANTIC_DIFFERENCES.md when a parity test surfaces
one.
"""

from typing import Any

import pandas as pd

from decoy_engine.graph.ops._base import OpError, is_polars_frame
from decoy_engine.internal.validator import ValidationError

KIND = "filter"
NATIVE_ENGINE = "polars"
INPUT_ARITY: tuple[int, int | None] = (1, 1)
OUTPUT_KIND = "stream"


def validate_config(config: dict[str, Any]) -> None:
    pred = config.get("predicate")
    if not isinstance(pred, str) or not pred.strip():
        raise ValidationError(
            "'predicate' must be a non-empty string", "config.predicate"
        )


def apply(inputs, config, ctx):
    df = inputs[0]
    predicate = config["predicate"]
    if is_polars_frame(df):
        return _apply_polars(df, predicate)
    return _apply_pandas(df, predicate)


def _apply_pandas(df: pd.DataFrame, predicate: str) -> pd.DataFrame:
    try:
        return df.query(predicate, engine="python")
    except Exception as exc:
        raise OpError(f"filter predicate failed ({predicate!r}): {exc}") from exc


def _apply_polars(df, predicate: str):
    """Evaluate the predicate via Polars' SQLContext.

    Polars SQL accepts the boolean-expression dialect pandas-query users
    write — `state == 'CA' and age >= 18` works as-is. Polars is strict
    about quoting (single quotes for strings only); the validator already
    rejected empty / non-string predicates, so the user-facing failure
    surface here is "the predicate is not valid SQL"."""
    import polars as pl

    try:
        sql = f"SELECT * FROM df WHERE {predicate}"
        with pl.SQLContext(df=df, eager=True) as ctx:
            return ctx.execute(sql)
    except Exception as exc:
        raise OpError(f"filter predicate failed ({predicate!r}): {exc}") from exc

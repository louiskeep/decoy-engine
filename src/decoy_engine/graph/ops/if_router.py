"""IF router op: two-output-port row router.

Evaluates `predicate` (a SQL WHERE expression) against each row and routes
rows into two output ports:
  pass -- rows where the predicate is truthy
  fail -- rows where the predicate is falsy

YAML config:
  predicate: "age >= 18 AND status = 'active'"

Returns {"pass": df_matching, "fail": df_not_matching}.

Sprint 6: replaced pl.SQLContext SQL string construction with pl.sql_expr().
The predicate is parsed into a Polars Expr; the fail partition uses ~expr
(bitwise NOT on a boolean Expr) instead of a NOT (...) SQL string.
See docs/security/sql-surfaces.md for the S608 surface history.
"""

import polars as pl

from decoy_engine.graph.ops._base import OpError, is_polars_frame

KIND = "if"
NATIVE_ENGINE = "polars"
INPUT_ARITY = (1, 1)
OUTPUT_KIND = "split"
OUTPUT_PORTS = ("pass", "fail")


def validate_config(config: dict) -> None:
    from decoy_engine.internal.validator import ValidationError

    pred = config.get("predicate")
    if not isinstance(pred, str) or not pred.strip():
        raise ValidationError(
            "predicate must be a non-empty string", "config.predicate"
        )


def apply(inputs: list, config: dict, ctx=None) -> dict:
    df = inputs[0] if inputs else None
    if df is None:
        return {"pass": None, "fail": None}

    predicate = config["predicate"].strip()

    if is_polars_frame(df):
        try:
            expr = pl.sql_expr(predicate)
            df_pass = df.filter(expr)
            df_fail = df.filter(~expr)
        except Exception as exc:
            raise OpError(f"if_router predicate failed: {exc}") from exc
        return {"pass": df_pass, "fail": df_fail}

    # Pandas fallback (pandas-mode graphs or forced-pandas engine).
    try:
        df_pass = df.query(predicate, engine="python")
        df_fail = df.drop(df_pass.index)
        return {
            "pass": df_pass.reset_index(drop=True),
            "fail": df_fail.reset_index(drop=True),
        }
    except Exception as exc:
        raise OpError(f"if_router predicate failed: {exc}") from exc

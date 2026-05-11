"""sql_run: execute a SQL string against the upstream DataFrame via DuckDB.

The power-user escape hatch. Drop a `sql_run` node onto the canvas,
write a SELECT, and have the result flow downstream as if it were any
other transform. Useful for:
  - one-off projections that don't fit the existing filter / sort /
    derive vocabulary cleanly ("SELECT a, b, a+b AS c FROM df")
  - aggregate-with-windowing patterns that the engine doesn't have
    dedicated ops for yet (Item 19's `aggregate` op covers the common
    cases when it ships)
  - inline schema massaging while the engine waits on shipping richer
    typed transforms

Config:
    sql: str   non-empty SQL statement. The upstream input is bound as
               the table `df`. Single-input only in v1; multi-input
               (FROM customers JOIN orders ...) lands when Item 19's
               `join` ships with explicit named-input semantics.

Variable resolution (`${var.X}`, `${iteration.value}`, etc.) happens at
the platform layer before the engine sees the YAML. By the time
`apply()` runs, the SQL string is fully resolved.

NATIVE_ENGINE='duckdb' so the runner hands us a pyarrow.Table. We
register it as `df` on a private in-memory DuckDB connection, execute,
return Arrow.
"""
from __future__ import annotations

from typing import Any

from decoy_engine.graph.ops._base import OpError
from decoy_engine.internal.validator import ValidationError

KIND = "sql_run"
NATIVE_ENGINE = "duckdb"
INPUT_ARITY: tuple[int, int | None] = (1, 1)
OUTPUT_KIND = "stream"


def validate_config(config: dict[str, Any]) -> None:
    sql = config.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        raise ValidationError(
            "'sql' must be a non-empty string", "config.sql"
        )


def apply(inputs, config, ctx):
    import duckdb

    sql = config["sql"]
    upstream = inputs[0]

    # In-memory DuckDB connection per op invocation. Cheap to create, no
    # cross-job state. Closed in the finally block.
    con = duckdb.connect(":memory:")
    try:
        # Register the upstream input as a relation named `df`. DuckDB
        # accepts pyarrow.Table directly via register(); the runner's
        # Arrow boundary already materialized the input into that shape
        # because NATIVE_ENGINE='duckdb'.
        try:
            con.register("df", upstream)
        except Exception as exc:
            raise OpError(
                f"sql_run: failed to register upstream input as `df`: {exc}"
            ) from exc

        # Execute. Catch + retype any DuckDB error as OpError so the
        # graph runner surfaces it consistently with other ops.
        #
        # `.to_arrow_table()` materializes the result fully; `.arrow()`
        # returns a RecordBatchReader which leaks the open DuckDB
        # connection past our finally block. Materialize here so we can
        # close the connection cleanly.
        try:
            return con.execute(sql).to_arrow_table()
        except duckdb.Error as exc:
            raise OpError(f"sql_run: SQL execution failed: {exc}") from exc
        except Exception as exc:
            # Anything not-a-DuckDB-error (e.g. memory error) gets the
            # same OpError treatment but with a less specific message.
            raise OpError(f"sql_run: unexpected error: {exc}") from exc
    finally:
        con.close()

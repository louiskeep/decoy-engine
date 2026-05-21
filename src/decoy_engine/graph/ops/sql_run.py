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
            raise OpError(_format_duckdb_error(sql, exc)) from exc
        except Exception as exc:
            # Anything not-a-DuckDB-error (e.g. memory error) gets the
            # same OpError treatment but with a less specific message.
            raise OpError(f"sql_run: unexpected error: {exc}") from exc
    finally:
        con.close()


# DuckDB error prefix -> human-readable category. The DuckDB error
# message itself leads with one of these (e.g. "Binder Error: ..."),
# which is precise but jargon-heavy. Mapping it to a short operator-
# facing phrase lets the error message lead with "what's wrong"
# instead of burying it behind a DuckDB taxonomy word.
_DUCKDB_ERROR_CATEGORIES = {
    "Parser Error": "syntax error",
    "Binder Error": "type or name mismatch",
    "Catalog Error": "missing table or column",
    "Conversion Error": "value type cast failed",
    "Constraint Error": "constraint violation",
    "Out of Memory Error": "out of memory",
    "IO Error": "I/O error",
}


def _format_duckdb_error(sql: str, exc: Exception) -> str:
    """Build the operator-facing error message for a failed sql_run.

    Leads with "invalid SQL" + a category hint so the log line answers
    the operator's first question ("is my SQL wrong?") without making
    them parse DuckDB's "Binder Error / Catalog Error / ..." taxonomy.
    Preserves the full DuckDB detail (with its LINE/caret marker)
    underneath because that's what locates the offending token.
    """
    raw = str(exc)
    # DuckDB messages start with "<Category>: ..." -- pull the prefix
    # to map to a human phrase. Fall back to a generic label when the
    # message doesn't match the known prefixes.
    category = "invalid SQL"
    for prefix, label in _DUCKDB_ERROR_CATEGORIES.items():
        if raw.startswith(prefix):
            category = f"invalid SQL ({label})"
            break
    # Truncate very long SQL so the log line stays readable. 200 chars
    # is plenty to see the shape; the operator's editor still has the
    # full text.
    sql_preview = sql if len(sql) <= 200 else sql[:197] + "..."
    return (
        f"sql_run: {category}.\n"
        f"SQL: {sql_preview}\n"
        f"{raw}"
    )

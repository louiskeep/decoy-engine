"""Read-only SQL discovery against on-disk Parquet tables.

Powers the platform's "data viewer database" feature: every saved data
table is a Parquet file on disk; users open the SQL tab on Data view and
write SELECT statements to slice, group, and aggregate across tables.

The public helper here is :func:`run_discovery_sql`. It:

  1. Opens a private in-memory DuckDB connection.
  2. Registers each entry of ``tables`` (a ``{name: parquet_path}`` mapping)
     as a DuckDB view via ``read_parquet`` so the planner can push down
     projections + predicates without materializing the full file.
  3. Rejects anything that isn't a single read-only statement -- no
     INSERT / UPDATE / DELETE / CREATE / DROP / ATTACH / COPY / PRAGMA.
  4. Executes and returns a list-of-dicts plus the column order.

DuckDB has a per-connection ``readonly=True`` flag but it only applies
to file-backed databases; the in-memory case still allows ``CREATE
TABLE`` etc. So we layer a statement-kind filter on top.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from decoy_engine.exceptions import DecoyError


class DiscoverySqlError(DecoyError):
    """Raised when a discovery SQL statement is rejected or fails."""


# Match the *first* non-comment, non-whitespace token. DuckDB accepts
# SQL comments in two forms (line `-- ...` and block `/* ... */`); we
# strip both before checking the leading keyword so a comment-prefixed
# SELECT is still allowed.
_COMMENT_LINE = re.compile(r"--[^\n]*")
_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.DOTALL)

# Allowed leading keywords. SELECT is the obvious one; WITH lets users
# write CTEs that ultimately project results. VALUES + TABLE (DuckDB's
# shorthand for `SELECT * FROM tbl`) are accepted because both produce
# a result set without side effects.
_ALLOWED_LEADERS = frozenset({"SELECT", "WITH", "VALUES", "TABLE"})

# Banned keywords anywhere in the statement. These are surface-level
# guards; the engine's own privilege model (in-memory DB, no ATTACH)
# means a determined caller can't actually mutate persistent state,
# but rejecting the keywords up front gives a clean error message and
# stops a user from accidentally writing a side-effecting CTE.
_BANNED = (
    r"\b(INSERT|UPDATE|DELETE|MERGE|REPLACE|TRUNCATE|"
    r"CREATE|ALTER|DROP|ATTACH|DETACH|COPY|EXPORT|IMPORT|"
    r"PRAGMA|INSTALL|LOAD|SET|RESET|CHECKPOINT|VACUUM|CALL|"
    r"BEGIN|COMMIT|ROLLBACK)\b"
)
_BANNED_RE = re.compile(_BANNED, re.IGNORECASE)


@dataclass(frozen=True)
class DiscoveryResult:
    """Materialized result of a discovery query.

    ``columns`` is the ordered list of output column names; ``rows`` is
    a list of dicts keyed by those names. Values are coerced to JSON-
    friendly Python types (str/int/float/bool/None) so the platform can
    return them directly through FastAPI without extra serialization.
    """
    columns: list[str]
    rows: list[dict[str, Any]]


def _strip_comments(sql: str) -> str:
    return _COMMENT_BLOCK.sub(" ", _COMMENT_LINE.sub(" ", sql)).strip()


def _validate_select_only(sql: str) -> None:
    """Reject anything that isn't a single read-only statement.

    Two checks: leading-keyword whitelist + banned-keyword scan. We
    also reject multi-statement input (`;` followed by more SQL) so a
    user can't sneak ``SELECT 1; DROP TABLE x`` past the leader check.
    """
    cleaned = _strip_comments(sql)
    if not cleaned:
        raise DiscoverySqlError("SQL statement is empty")

    # Strip a single trailing semicolon if present; reject anything
    # beyond it. DuckDB accepts trailing `;` so this is the only
    # split-point a user would naturally hit.
    body = cleaned.rstrip(";").strip()
    if ";" in body:
        raise DiscoverySqlError(
            "Multiple statements are not allowed. Run one SELECT at a time."
        )

    leader = body.split(None, 1)[0].upper() if body else ""
    if leader not in _ALLOWED_LEADERS:
        raise DiscoverySqlError(
            f"Only read-only queries are allowed (SELECT / WITH / VALUES / TABLE). "
            f"Got: {leader or '<empty>'}"
        )

    match = _BANNED_RE.search(body)
    if match:
        raise DiscoverySqlError(
            f"Keyword '{match.group(0).upper()}' is not allowed in discovery queries."
        )


def _coerce(value: Any) -> Any:
    """Coerce a DuckDB Python value to a JSON-friendly form.

    DuckDB returns native Python objects for most scalar types; the
    handful that aren't (datetime, Decimal, bytes) get isoformat /
    string conversion so the platform can serialize results directly.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    # datetime/date/time all expose isoformat().
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
    return str(value)


def run_discovery_sql(
    sql: str,
    tables: Mapping[str, str],
    *,
    row_limit: int = 10_000,
) -> DiscoveryResult:
    """Run a SELECT-only SQL statement against on-disk Parquet tables.

    ``tables`` maps table name -> absolute Parquet path. Each name is
    registered as a DuckDB view via ``read_parquet`` so the planner
    can prune columns + push down predicates. ``row_limit`` caps the
    number of returned rows -- the query itself is unbounded, but we
    truncate the materialized result to keep the response small.

    Raises ``DiscoverySqlError`` on validation failure or DuckDB
    execution error.
    """
    import duckdb  # noqa: PLC0415 -- keeps duckdb off the import path for callers that don't need it

    _validate_select_only(sql)

    con = duckdb.connect(":memory:")
    try:
        for name, path in tables.items():
            # Quote the view name with double quotes; DuckDB treats
            # double-quoted identifiers as case-sensitive but accepts
            # any printable string. We use parameter binding for the
            # path so a malicious path can't break out of the
            # read_parquet call.
            safe = name.replace('"', '""')
            con.execute(
                f'CREATE OR REPLACE VIEW "{safe}" AS SELECT * FROM read_parquet(?)',
                [path],
            )

        try:
            rel = con.execute(sql)
        except duckdb.Error as exc:
            raise DiscoverySqlError(f"SQL execution failed: {exc}") from exc

        cols = [d[0] for d in rel.description]
        # fetchmany pulls at most row_limit rows; we don't materialize
        # everything in case the user wrote `SELECT * FROM huge`.
        raw = rel.fetchmany(row_limit)
        rows = [
            {col: _coerce(value) for col, value in zip(cols, row)}
            for row in raw
        ]
        return DiscoveryResult(columns=cols, rows=rows)
    finally:
        con.close()


__all__ = [
    "DiscoveryResult",
    "DiscoverySqlError",
    "run_discovery_sql",
]

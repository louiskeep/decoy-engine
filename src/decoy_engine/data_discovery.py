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
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from decoy_engine.errors import DecoyError


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
    r"BEGIN|COMMIT|ROLLBACK|"
    # QA-2 (2026-05-31): DuckDB file-reading table functions. The
    # discovery surface is supposed to project from the registered
    # staged-Parquet view only; allowing read_csv('/etc/passwd')
    # turns the discovery helper into an arbitrary-file-read
    # surface. Source: docs/audit/dennis-qa-triage-2026-05-31.md M20.
    # Out of scope: DuckDB extension functions (httpfs, iceberg_scan)
    # not installed in V1 deployments; add when those land.
    r"read_csv|read_csv_auto|read_parquet|read_json|read_ndjson)\b"
)
_BANNED_RE = re.compile(_BANNED, re.IGNORECASE)

# QA-2 (2026-05-31): DuckDB accepts FROM '/path/to/file' as a table
# reference (auto-detects format). The function-call denylist above
# does not catch this shape; reject quoted-path leading-FROM
# explicitly. Source: docs/audit/dennis-qa-triage-2026-05-31.md M20.
_QUOTED_PATH_FROM_RE = re.compile(r"\bFROM\s+['\"]", re.IGNORECASE)


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

    Three checks: leading-keyword whitelist + banned-keyword scan +
    quoted-path-FROM scan. The quoted-path scan (QA-2, 2026-05-31)
    closes a DuckDB-specific gap where ``FROM '/path/to/file'`` would
    bypass the banned-keyword scan and read arbitrary files. The
    banned-keyword scan also rejects DuckDB file-reading table
    functions (`read_csv`, `read_csv_auto`, `read_parquet`,
    `read_json`, `read_ndjson`).

    Source: QA-2 (2026-05-31); QA-triage M20
    (`docs/audit/dennis-qa-triage-2026-05-31.md`).
    """
    cleaned = _strip_comments(sql)
    if not cleaned:
        raise DiscoverySqlError("SQL statement is empty")

    # Strip a single trailing semicolon if present; reject anything
    # beyond it. DuckDB accepts trailing `;` so this is the only
    # split-point a user would naturally hit.
    body = cleaned.rstrip(";").strip()
    if ";" in body:
        raise DiscoverySqlError("Multiple statements are not allowed. Run one SELECT at a time.")

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
    if _QUOTED_PATH_FROM_RE.search(body):
        raise DiscoverySqlError(
            "FROM clause must reference an identifier or registered view, not a quoted path."
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
    import duckdb

    _validate_select_only(sql)

    con = duckdb.connect(":memory:")
    try:
        for name, path in tables.items():
            # DuckDB rejects `?` parameter binding inside CREATE VIEW
            # at the binder layer ("Unexpected prepared parameter").
            # Use the Python relational API instead: read_parquet()
            # takes the path as a typed Python argument (no SQL string
            # concatenation, no injection surface), and create_view()
            # registers the resulting relation under a name that DuckDB
            # quotes internally.
            con.read_parquet(path).create_view(name, replace=True)

        try:
            rel = con.execute(sql)
        except duckdb.Error as exc:
            raise DiscoverySqlError(f"SQL execution failed: {exc}") from exc

        cols = [d[0] for d in rel.description]
        # fetchmany pulls at most row_limit rows; we don't materialize
        # everything in case the user wrote `SELECT * FROM huge`.
        raw = rel.fetchmany(row_limit)
        rows = [{col: _coerce(value) for col, value in zip(cols, row, strict=False)} for row in raw]
        return DiscoveryResult(columns=cols, rows=rows)
    finally:
        con.close()


__all__ = [
    "DiscoveryResult",
    "DiscoverySqlError",
    "run_discovery_sql",
]

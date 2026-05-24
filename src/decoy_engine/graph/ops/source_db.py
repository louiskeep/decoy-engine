"""source.db -- read a table from a SQL database into a DataFrame.

Config:
    table: str             - table name (required)
    schema: str            - optional, db-specific
    where: str             - optional SQL WHERE clause (no leading "WHERE")
    dsn: str               - direct SQLAlchemy DSN (CLI path)
    connector_id: int      - platform path; resolved via ctx.resolve_connector

Exactly one of `dsn` or `connector_id` must be present. When both are
provided, `dsn` wins.

NATIVE_ENGINE='duckdb'. The DuckDB path dispatches on DSN dialect:

- **SQLite** -> DuckDB `sqlite_scanner` extension via `ATTACH (TYPE sqlite)`.
  Native streaming, no pandas materialization at the read boundary.
- **Postgres** -> DuckDB `postgres_scanner` via `ATTACH (TYPE postgres)`.
  Same shape; needs a running Postgres for tests.
- **Everything else** (MySQL until customer signal, MSSQL, Oracle, etc.)
  -> SQLAlchemy + Arrow fallback. Materializes through pandas; works
  but doesn't deliver the streaming benefit.

The dispatch is per Bug 3 in `plans/2026-05-09-hybrid-engine-bug-followup.md`.
Adding a new dialect to the native-scanner path is a one-row table edit
in `_NATIVE_SCANNERS` plus a connection-string converter if the DSN
shape differs from DuckDB's `ATTACH` expectation.

SECURITY (Sprint 6):
- Table and schema identifiers are validated by _validate_sql_identifier()
  before entering any SQL string. This rejects double-quote injection and
  semicolons at config validation time.
- For native-scanner paths (SQLite, Postgres): the user-supplied 'where'
  value is applied via the DuckDB relational API (.filter()), which parses
  the expression without constructing a full SQL statement. UNION/stacked-
  query injection is not possible through this path.
- For the SQLAlchemy fallback (non-SQLite/Postgres dialects): 'where' is
  still concatenated into SQL. These paths treat 'where' as a SQL-literate
  config field at the same trust level as 'table' and 'schema'. Identifier
  names are validated. See docs/security/sql-surfaces.md.
"""

import re
from typing import Any

import pandas as pd
import pyarrow as pa

from decoy_engine.errors import ValidationError
from decoy_engine.graph.ops._base import OpError

# (sqlalchemy_dialect_prefix, duckdb_extension, duckdb_attach_type).
_NATIVE_SCANNERS: dict[str, tuple[str, str]] = {
    "sqlite": ("sqlite_scanner", "sqlite"),
    "postgresql": ("postgres_scanner", "postgres"),
}

# Identifiers (table, schema) must match this pattern before entering
# any SQL string. Rejects double-quote injection, semicolons, spaces,
# and other SQL metacharacters that can break the quoting boundary.
_SAFE_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

KIND = "source.db"
NATIVE_ENGINE = "duckdb"
INPUT_ARITY: tuple[int, int | None] = (0, 0)
OUTPUT_KIND = "stream"


def _validate_sql_identifier(name: str, path: str) -> None:
    """Raise ValidationError if name contains SQL metacharacters.

    Exported so target_db.py can reuse the same check without duplicating
    the regex or the error message.
    """
    if not _SAFE_ID_RE.match(name):
        raise ValidationError(
            f"SQL identifier {name!r} contains disallowed characters "
            "(only letters, digits, underscores, and $ are permitted)",
            path,
        )


def validate_config(config: dict[str, Any]) -> None:
    if "table" not in config:
        raise ValidationError("missing required field 'table'", "config.table")
    _validate_sql_identifier(config["table"], "config.table")
    if config.get("schema"):
        _validate_sql_identifier(config["schema"], "config.schema")
    if not config.get("dsn") and config.get("connector_id") is None:
        raise ValidationError("must provide either 'dsn' or 'connector_id'", "config")


def apply(inputs, config, ctx):
    engine = config.get("__engine", "pandas")
    if engine == "duckdb":
        return _apply_duckdb(config, ctx)
    return _apply_pandas(config, ctx)


def _apply_pandas(config: dict[str, Any], ctx) -> pd.DataFrame:
    dsn = _resolve_dsn(config, ctx)
    sql = _build_select(config)

    try:
        from sqlalchemy import create_engine

        engine = create_engine(dsn)
        try:
            return pd.read_sql(sql, engine)
        finally:
            engine.dispose()
    except Exception as exc:
        raise OpError(f"source.db read failed: {exc}") from exc


def _apply_duckdb(config: dict[str, Any], ctx) -> pa.Table:
    dsn = _resolve_dsn(config, ctx)
    scanner = _resolve_scanner(dsn)
    if scanner is not None:
        return _apply_duckdb_native_scanner(scanner, dsn, config)
    return _apply_duckdb_sqlalchemy_fallback(dsn, config)


def _apply_duckdb_native_scanner(
    scanner: tuple[str, str],
    dsn: str,
    config: dict[str, Any],
) -> pa.Table:
    """DuckDB consumes the source DB directly via its scanner extension.

    The base relation is built from the double-quoted (validated) table name
    only. The user-supplied WHERE expression is applied through the DuckDB
    relational API (.filter()), which parses it as a filter expression rather
    than concatenating it into a full SQL string. This prevents UNION and
    stacked-query injection through the 'where' config field.
    """
    extension, attach_type = scanner
    attach_target = _attach_target_for(dsn, attach_type)

    table = config["table"]
    schema = config.get("schema") or None
    where = config.get("where")
    row_limit = config.get("__preview_row_limit")

    # Alias under which the remote DB is attached. Per-call so we don't
    # collide if multiple source.db ops run in the same process.
    alias = "src"
    qualified = f'{alias}."{schema}"."{table}"' if schema else f'{alias}."{table}"'

    try:
        import duckdb

        con = duckdb.connect()
        try:
            con.execute(f"INSTALL {extension}")
            con.execute(f"LOAD {extension}")
            con.execute(f"ATTACH '{attach_target}' AS {alias} (TYPE {attach_type}, READ_ONLY)")
            # Base relation: only validated identifier names in the SQL string.
            rel = con.sql(f"SELECT * FROM {qualified}")
            if where:
                # Relational API filter: 'where' is parsed as a filter
                # expression against the relation's columns -- not
                # concatenated into a full SQL statement.
                rel = rel.filter(where)
            if row_limit:
                rel = rel.limit(int(row_limit))
            # Materialize to a pa.Table. As of DuckDB 1.5.x, rel.arrow()
            # returns a RecordBatchReader rather than a Table, which
            # breaks every downstream consumer that expects a Table
            # (cache.write_stream, parity test _to_pd, pd.DataFrame
            # init). to_arrow_table() is the explicit materialized form
            # -- the same lesson is documented in source_file.py and
            # sql_run.py where this regression already bit once.
            return rel.to_arrow_table()
        finally:
            con.close()
    except Exception as exc:
        raise OpError(f"source.db read failed: {exc}") from exc


def _apply_duckdb_sqlalchemy_fallback(dsn: str, config: dict[str, Any]) -> pa.Table:
    """For DBs without a DuckDB native scanner (MSSQL, Oracle, etc.):
    SQLAlchemy executes the SELECT and we convert the resulting
    DataFrame to Arrow at the boundary.
    """
    sql = _build_select(config)
    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(dsn)
        try:
            with engine.connect() as conn:
                df = pd.read_sql(text(sql), conn)
            return pa.Table.from_pandas(df, preserve_index=False)
        finally:
            engine.dispose()
    except Exception as exc:
        raise OpError(f"source.db read failed: {exc}") from exc


def _resolve_scanner(dsn: str) -> tuple[str, str] | None:
    """Return the DuckDB scanner tuple (extension, attach_type) for this
    DSN, or None if no native scanner is available."""
    dialect = dsn.split(":", 1)[0].split("+", 1)[0].lower()
    return _NATIVE_SCANNERS.get(dialect)


def _attach_target_for(dsn: str, attach_type: str) -> str:
    """Convert a SQLAlchemy DSN to the connection string DuckDB's
    `ATTACH` expects.

    SQLite: `sqlite:///path/to.db` -> `path/to.db`.
    Postgres: `postgresql://user:pass@host:5432/dbname` -> libpq key=value.
    """
    if attach_type == "sqlite":
        for prefix in ("sqlite:///", "sqlite://"):
            if dsn.startswith(prefix):
                return dsn[len(prefix) :]
        return dsn

    if attach_type == "postgres":
        from urllib.parse import unquote, urlparse

        parsed = urlparse(dsn)
        parts: list[str] = []
        if parsed.path and len(parsed.path) > 1:
            parts.append(f"dbname={parsed.path[1:]}")
        if parsed.hostname:
            parts.append(f"host={parsed.hostname}")
        if parsed.port:
            parts.append(f"port={parsed.port}")
        if parsed.username:
            parts.append(f"user={unquote(parsed.username)}")
        if parsed.password:
            parts.append(f"password={unquote(parsed.password)}")
        return " ".join(parts)

    return dsn


def _build_select(config: dict[str, Any]) -> str:
    """Build a SELECT statement for the SQLAlchemy fallback paths.

    Used only for non-SQLite/Postgres dialects (MSSQL, Oracle, etc.).
    The 'where' field is treated as a SQL-literate config value at the
    same trust level as 'table' and 'schema'. Identifiers are validated
    by validate_config() before this function is reached.
    Native-scanner paths (SQLite, Postgres) use the DuckDB relational API
    and do not call this function.
    """
    table = config["table"]
    schema = config.get("schema") or None
    where = config.get("where")
    row_limit = config.get("__preview_row_limit")

    qualified = f'"{schema}"."{table}"' if schema else f'"{table}"'
    sql = f"SELECT * FROM {qualified}"
    if where:
        sql += f" WHERE {where}"
    if row_limit:
        sql += f" LIMIT {int(row_limit)}"
    return sql


def _resolve_dsn(config: dict[str, Any], ctx) -> str:
    if config.get("dsn"):
        return config["dsn"]
    if ctx is None or getattr(ctx, "resolve_connector", None) is None:
        raise OpError(
            "connector_id provided but ctx.resolve_connector is None "
            "(CLI must use inline 'dsn' instead)"
        )
    return ctx.resolve_connector(int(config["connector_id"]))

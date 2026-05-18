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

SECURITY: the 'where' config value is concatenated into raw SQL against an
external database. This is a known S608 surface documented in
docs/security/sql-surfaces.md. Planned fix: Sprint 6 (DuckDB relational API
or parsed boolean-expression allowlist).
"""

from typing import Any

import pandas as pd
import pyarrow as pa

from decoy_engine.graph.ops._base import OpError
from decoy_engine.internal.validator import ValidationError


# (sqlalchemy_dialect_prefix, duckdb_extension, duckdb_attach_type).
# A DSN like `sqlite:///path` matches "sqlite"; `postgresql+psycopg://...`
# matches "postgresql" via prefix. Anything else falls through to the
# SQLAlchemy bridge.
_NATIVE_SCANNERS: dict[str, tuple[str, str]] = {
    "sqlite": ("sqlite_scanner", "sqlite"),
    "postgresql": ("postgres_scanner", "postgres"),
}

KIND = "source.db"
NATIVE_ENGINE = "duckdb"
INPUT_ARITY: tuple[int, int | None] = (0, 0)
OUTPUT_KIND = "stream"


def validate_config(config: dict[str, Any]) -> None:
    if "table" not in config:
        raise ValidationError("missing required field 'table'", "config.table")
    if not config.get("dsn") and config.get("connector_id") is None:
        raise ValidationError(
            "must provide either 'dsn' or 'connector_id'", "config"
        )


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
    The remote DB is `ATTACH`ed under an alias; we then issue a single
    SELECT against the alias-prefixed table name and let DuckDB stream
    the read into Arrow.
    """
    extension, attach_type = scanner
    attach_target = _attach_target_for(dsn, attach_type)

    table = config["table"]
    schema = config.get("schema") or None
    where = config.get("where")
    row_limit = config.get("__preview_row_limit")

    # Alias under which the remote DB is attached. Per-call so we don't
    # collide if multiple source.db ops run in the same process. DuckDB
    # auto-detaches when the connection closes.
    alias = "src"
    qualified = (
        f'{alias}."{schema}"."{table}"' if schema else f'{alias}."{table}"'
    )
    sql = f"SELECT * FROM {qualified}"
    if where:
        # S608: 'where' is user-supplied YAML config concatenated into SQL
        # executed against an external database. HIGH risk surface.
        # See docs/security/sql-surfaces.md. Fix planned for Sprint 6.
        sql += f" WHERE {where}"  # noqa: S608
    if row_limit:
        sql += f" LIMIT {int(row_limit)}"

    try:
        import duckdb

        con = duckdb.connect()
        try:
            # INSTALL is idempotent + cached after first download; LOAD
            # activates it for this connection.
            con.execute(f"INSTALL {extension}")
            con.execute(f"LOAD {extension}")
            con.execute(
                f"ATTACH '{attach_target}' AS {alias} (TYPE {attach_type}, READ_ONLY)"
            )
            # `to_arrow_table()` is the explicit stable API; `.arrow()`
            # returned a RecordBatchReader on duckdb 1.5.x in our test env.
            return con.execute(sql).to_arrow_table()
        finally:
            con.close()
    except Exception as exc:
        raise OpError(f"source.db read failed: {exc}") from exc


def _apply_duckdb_sqlalchemy_fallback(
    dsn: str, config: dict[str, Any]
) -> pa.Table:
    """For DBs without a DuckDB native scanner (MSSQL, Oracle, etc.):
    SQLAlchemy executes the SELECT and we convert the resulting
    DataFrame to Arrow at the boundary. Pays the materialization cost
    that the native-scanner path avoids -- but works on every dialect
    SQLAlchemy supports.
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

    SQLite: `sqlite:///path/to.db` -> `path/to.db`. The triple slash
    introduces an absolute path on Unix; on Windows the drive letter
    follows the third slash. DuckDB just wants the plain path.

    Postgres: `postgresql://user:pass@host:5432/dbname` ->
    `dbname=dbname host=host port=5432 user=user password=pass`. DuckDB's
    postgres_scanner uses libpq-style key=value pairs. We rebuild the
    connection string from the parsed URL components.
    """
    if attach_type == "sqlite":
        # Strip dialect prefix; SQLAlchemy uses 3 slashes for absolute
        # paths on Unix and varies on Windows.
        # sqlite:////abs/path falls through to sqlite:/// so the leading
        # slash of the absolute path is preserved.
        for prefix in ("sqlite:///", "sqlite://"):
            if dsn.startswith(prefix):
                return dsn[len(prefix):]
        return dsn  # fallback: pass through

    if attach_type == "postgres":
        from urllib.parse import urlparse, unquote

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
    table = config["table"]
    schema = config.get("schema") or None
    where = config.get("where")
    row_limit = config.get("__preview_row_limit")

    qualified = f'"{schema}"."{table}"' if schema else f'"{table}"'
    sql = f"SELECT * FROM {qualified}"
    if where:
        # S608: 'where' is user-supplied YAML config concatenated into SQL.
        # See docs/security/sql-surfaces.md. Fix planned for Sprint 6.
        sql += f" WHERE {where}"  # noqa: S608
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

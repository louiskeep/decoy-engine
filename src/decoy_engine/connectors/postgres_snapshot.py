"""Postgres snapshot-to-Parquet materialization primitive.

Reads a fixed, structured selection of Postgres relations through DuckDB's
``postgres`` extension (the "postgres scanner",
https://duckdb.org/docs/stable/core_extensions/postgres/overview) and stages
each relation as one Parquet file. This is a materialization step, not a
streaming connector: DuckDB owns the wire protocol and type mapping, Decoy
owns only the narrow, read-only, structured selection-to-Parquet step.

D0: this module is now the **Postgres adapter** of the shared DuckDB-attach
seam. The dialect-independent orchestration, models, SQL helpers, and error base
live in ``decoy_engine.connectors.duckdb_attach``; everything below is the
Postgres-specific ``Dialect`` plus the public surface preserved for callers
(``PostgresSnapshotConfig`` / ``PostgresSnapshotError`` / ``RelationSelection`` /
``snapshot_tables_to_parquet``). Behavior is byte-identical to the pre-D0 module.

Pinned guarantees (each has a test in ``tests/connectors/test_postgres_snapshot*``):

* One snapshot per call. All relations are exported inside one DuckDB
  transaction over one attached database, so a parent/child pair that is
  mutated between two exports is either wholly in or wholly out of the staged
  set. Postgres serves this from a single REPEATABLE READ snapshot that the
  scanner's parallel ctid workers import.
* Structured input only. Callers pass identifiers and column names, never SQL.
  Every identifier is validated against a strict pattern and then quoted; no
  value is concatenated into SQL unquoted.
* Deterministic output. Each export is ``ORDER BY`` the caller's key columns
  followed by every other projected column, ``NULLS LAST``, so equal source
  state yields identical Parquet even for keyless relations or non-unique
  keys. DuckDB's scan order is not relied on.
* Atomic publish. Files are written to a private staging directory and the
  directory is renamed into place only after every relation succeeded. Any
  failure, timeout, or interrupt removes staged and spill files and returns
  nothing.
* Type fidelity. A column whose Postgres type DuckDB cannot carry without
  loss is rejected with a coded error before any row is read. DuckDB's scanner
  otherwise degrades several types silently (unbounded or >38 digit numeric to
  DOUBLE, numeric NaN to NULL, far timestamps wrapping around int64,
  infinity to a sentinel), which a masking pipeline must never ingest.
* Offline extension. The extension is loaded from a caller-supplied directory
  with auto-install and auto-load disabled. The primitive never reaches the
  network to fetch it, and fails closed with a coded error if it is absent or
  incompatible.
* No leaks. DuckDB echoes the full connection string in connect errors, so
  raw exception text is never forwarded or chained. Errors and logs carry a
  code, a relation or column location, and the exception type only.

The library does not know its callers: it takes a resolved DSN as a
``SecretStr`` and knows nothing about accounts or credential references.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

# Dialect-independent core (D0). Re-exported names keep the public + private
# surface this module has always exposed (callers import several privates, and
# the integration suite monkeypatches `_export_relation` / `_open_connection`
# as module globals -- preserved here as bare module functions). `_IDENT_RE` and
# `_validate_ident` are re-exported for compatibility only.
from decoy_engine.connectors.duckdb_attach import (
    _IDENT_RE,  # noqa: F401
    RelationSelection,
    SnapshotConfig,
    SnapshotError,
    _lit,
    _order_clause,
    _q,
    _validate_ident,  # noqa: F401
    run_snapshot,
)

__all__ = [
    "PostgresSnapshotConfig",
    "PostgresSnapshotError",
    "RelationSelection",
    "snapshot_tables_to_parquet",
]

# Alias for the attached database. Fixed, never caller-derived.
_ATTACH_ALIAS = "decoy_pg_src"

# DuckDB TIMESTAMP is int64 microseconds. Postgres allows years up to 294276
# AD, which wraps silently on the scanner's int64 conversion. The guard window
# keeps a wide safety margin inside DuckDB's range and still covers Postgres'
# whole BC range.
_TS_MIN = "4713-01-01 BC"
_TS_MAX = "290000-12-31 23:59:59"

_NUMERIC_MAX_PRECISION = 38  # DuckDB DECIMAL ceiling

# Scalar Postgres types the scanner carries without loss, keyed by the
# ``format_type`` text with any ``(n)`` length suffix removed. ``character(n)``
# is absent on purpose: the scanner trims its blank padding, which is a value
# change.
_PLAIN_TYPES = frozenset(
    {
        "smallint",
        "integer",
        "bigint",
        "real",
        "double precision",
        "boolean",
        "text",
        "character varying",
        '"char"',
        "uuid",
        "json",
        "jsonb",
        "bytea",
        "time without time zone",
    }
)
_DATE = "date"
_TIMESTAMPS = frozenset({"timestamp without time zone", "timestamp with time zone"})
_NUMERIC_RE = re.compile(r"^numeric\((\d+),(\d+)\)$")
_LEN_SUFFIX_RE = re.compile(r"\(\d+\)$")


class PostgresSnapshotConfig(SnapshotConfig):
    """Connection and resource policy for one Postgres snapshot call.

    The shared :class:`SnapshotConfig`; named for the public API and so a future
    Postgres-only field has a home.
    """


class PostgresSnapshotError(SnapshotError):
    """Coded, redacted snapshot failure. Message: ``postgres_snapshot[code] …``."""

    _PREFIX = "postgres_snapshot"


# ----- extension loading --------------------------------------------------


def _open_connection(config: PostgresSnapshotConfig, spill_dir: Path) -> Any:
    import duckdb

    spill_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(spill_dir, 0o700)
    try:
        conn = duckdb.connect(
            database=":memory:",
            config={
                "temp_directory": str(spill_dir),
                "memory_limit": config.memory_limit,
                "threads": config.threads,
                "extension_directory": str(config.extension_directory),
                # Fail closed offline: nothing may be fetched or auto-loaded.
                "autoinstall_known_extensions": False,
                "autoload_known_extensions": False,
                "allow_unsigned_extensions": False,
                # Kept True so the ORDER BY result order survives the Parquet
                # write even when the export spills; dropping it would let DuckDB
                # reorder spilled chunks and break the byte-identical snapshot
                # (guarantee 2).
                "preserve_insertion_order": True,
            },
        )
    except Exception as exc:
        raise PostgresSnapshotError("duckdb_unavailable", cause_type=type(exc).__name__) from None
    return conn


def _load_postgres_extension(conn: Any, config: PostgresSnapshotConfig) -> None:
    ext_dir = config.extension_directory
    try:
        conn.execute("LOAD postgres")
        row = conn.execute(
            "SELECT loaded, install_path FROM duckdb_extensions() WHERE extension_name = 'postgres_scanner'"
        ).fetchone()
    except Exception as exc:
        raise PostgresSnapshotError(
            "extension_unavailable", cause_type=type(exc).__name__
        ) from None
    if not row or not row[0]:
        raise PostgresSnapshotError("extension_unavailable")
    installed_under = str(row[1] or "")
    # An extension that loaded from anywhere but the bundled directory means the
    # offline pin was bypassed, so refuse it.
    if not installed_under.startswith(str(ext_dir)):
        raise PostgresSnapshotError("extension_untrusted_location")


# ----- type fidelity ------------------------------------------------------

# Constant catalog query: no caller text is interpolated. format_type gives
# the declared type (with precision), and the element typtype lets enum
# arrays be told apart from other arrays.
_CATALOG_SQL = (
    "SELECT n.nspname, c.relname, a.attname, format_type(a.atttypid, a.atttypmod), "
    "COALESCE(e.typtype, t.typtype)::text "
    "FROM pg_attribute a "
    "JOIN pg_class c ON c.oid = a.attrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "JOIN pg_type t ON t.oid = a.atttypid "
    "LEFT JOIN pg_type e ON e.oid = t.typelem AND t.typcategory = 'A' "
    "WHERE a.attnum > 0 AND NOT a.attisdropped "
    "AND c.relkind IN ('r', 'p', 'v', 'm', 'f') "
    "AND n.nspname NOT IN ('pg_catalog', 'information_schema')"
)


def _classify(type_text: str, typtype: str) -> str | None:
    """Return a guard class for a supported type, or None if unsupported.

    Classes: ``plain`` (no guard needed), ``array`` (lower-bound guard), ``date``, ``timestamp``,
    ``numeric`` (bounded <= 38 digits; NaN guard).
    """
    base = type_text
    is_array = False
    while base.endswith("[]"):
        base = base[:-2]
        is_array = True
    base_nolen = _LEN_SUFFIX_RE.sub("", base)
    if typtype == "e" or base_nolen in _PLAIN_TYPES:
        # Arrays need a lower-bound guard: the scanner drops a non-1 lower
        # bound, so '[0:2]={..}' would export with its indexing lost.
        return "array" if is_array else "plain"
    # Temporal and numeric element guards run on scalar columns only. An array
    # of them would need an unnest guard, so it is rejected instead.
    if is_array:
        return None
    if base == _DATE:
        return "date"
    if _LEN_SUFFIX_RE.sub("", base) in _TIMESTAMPS:
        return "timestamp"
    m = _NUMERIC_RE.fullmatch(base)
    if m and int(m.group(1)) <= _NUMERIC_MAX_PRECISION:
        return "numeric"
    return None


def _check_types(conn: Any, sel: RelationSelection) -> dict[str, str]:
    """Verify columns exist and are lossless. Returns {column: guard class}."""
    try:
        rows = conn.execute(
            f"SELECT nspname, relname, attname, fmt, typtype FROM postgres_query({_lit(_ATTACH_ALIAS)}, ?) "
            "AS q(nspname, relname, attname, fmt, typtype) "
            "WHERE lower(nspname) = lower(?) AND lower(relname) = lower(?)",
            [_CATALOG_SQL, sel.schema_name, sel.table],
        ).fetchall()
    except Exception as exc:
        raise PostgresSnapshotError(
            "catalog_read_failed", location=sel.key, cause_type=type(exc).__name__
        ) from None
    # DuckDB resolves schema, table and column names case-insensitively, so a
    # source with names differing only by case would export the wrong object
    # without any error. Fetch case-insensitive matches and reject ambiguity,
    # then require an exact-case match so a near miss is never resolved.
    if len({r[0] for r in rows}) > 1 or len({(r[0], r[1]) for r in rows}) > 1:
        raise PostgresSnapshotError("ambiguous_name", location=sel.key)
    rows = [r for r in rows if r[0] == sel.schema_name and r[1] == sel.table]
    if not rows:
        raise PostgresSnapshotError("relation_not_found", location=sel.key)
    folded: dict[str, int] = {}
    for r in rows:
        folded[r[2].lower()] = folded.get(r[2].lower(), 0) + 1
    # Reject the relation if ANY two of its columns collide case-insensitively,
    # even ones outside this projection. DuckDB folds a case-colliding pair to
    # one name and renames the duplicate with a generated suffix (a + "A" ->
    # a, A_1), which can then shadow a real column like a_1 and export the
    # wrong column with no error. The whole relation is unsafe, so refuse it.
    if any(count > 1 for count in folded.values()):
        raise PostgresSnapshotError("ambiguous_name", location=sel.key)
    declared = {r[2]: (r[3], r[4]) for r in rows}
    classes: dict[str, str] = {}
    for col in sel.columns:
        if col not in declared:
            raise PostgresSnapshotError("column_not_found", location=f"{sel.key}.{col}")
        cls = _classify(*declared[col])
        if cls is None:
            raise PostgresSnapshotError("unsupported_column_type", location=f"{sel.key}.{col}")
        classes[col] = cls
    return classes


def _guard_values(conn: Any, sel: RelationSelection, classes: dict[str, str]) -> None:
    """Reject values DuckDB would alter silently, before any export.

    Runs through ``postgres_query`` on the same attached connection and inside
    the same transaction as the exports, so it is expected to read from that
    transaction's snapshot. That participation is not gate-tested (gate 1
    exercises the ATTACH scan path), and a spike showed it for a trivial query
    only. Until a mutation test covers it, treat a value changing between the
    guard and a later export as an unverified edge, tracked for Slice 2. The
    SQL is built only from validated, quoted identifiers and fixed literals.
    """
    checks: list[tuple[str, str]] = []
    for col, cls in classes.items():
        qc = _q(col)
        if cls == "date":
            cond = f"NOT isfinite({qc})"
        elif cls == "timestamp":
            cond = (
                f"(NOT isfinite({qc}) OR {qc} < '{_TS_MIN}'::timestamp "
                f"OR {qc} > '{_TS_MAX}'::timestamp)"
            )
        elif cls == "array":
            # array_lower is NULL for empty arrays, which are fine. Postgres
            # allows at most 6 dimensions.
            cond = " OR ".join(f"array_lower({qc}, {d}) <> 1" for d in range(1, 7))
        elif cls == "numeric":
            # DuckDB's scanner maps numeric NaN to NULL by default.
            cond = f"{qc} = 'NaN'::numeric"
        else:
            continue
        checks.append((col, f"count(*) FILTER (WHERE {qc} IS NOT NULL AND {cond})"))
    if not checks:
        return
    sql = (
        "SELECT "
        + ", ".join(f"{expr} AS g{i}" for i, (_, expr) in enumerate(checks))
        + f" FROM {_q(sel.schema_name)}.{_q(sel.table)}"
    )
    try:
        counts = conn.execute(
            f"SELECT * FROM postgres_query({_lit(_ATTACH_ALIAS)}, ?)", [sql]
        ).fetchone()
    except Exception as exc:
        raise PostgresSnapshotError(
            "value_guard_failed", location=sel.key, cause_type=type(exc).__name__
        ) from None
    for (col, _), n in zip(checks, counts or (), strict=True):
        if n:
            raise PostgresSnapshotError("value_not_representable", location=f"{sel.key}.{col}")


# ----- export -------------------------------------------------------------


def _export_relation(conn: Any, sel: RelationSelection, target: Path, row_group_size: int) -> None:
    cols = ", ".join(_q(c) for c in sel.columns)
    src = f"{_q(_ATTACH_ALIAS)}.{_q(sel.schema_name)}.{_q(sel.table)}"
    conn.execute(
        f"COPY (SELECT {cols} FROM {src} ORDER BY {_order_clause(sel)}) "
        f"TO {_lit(str(target))} (FORMAT parquet, ROW_GROUP_SIZE {int(row_group_size)})"
    )


# ----- the Postgres dialect + public entrypoint ---------------------------


class _PostgresDialect:
    """The Postgres hooks for ``duckdb_attach.run_snapshot``.

    The connection / extension / export hooks call this module's bare globals
    (``_open_connection`` etc.) so the integration suite's
    ``monkeypatch.setattr(ps, "_export_relation", …)`` still reaches them; type
    and attach hooks are Postgres-specific and live here directly.
    """

    log_label = "postgres_snapshot"
    attach_alias = _ATTACH_ALIAS

    def error(self, code: str, *, location: str = "", cause_type: str = "") -> PostgresSnapshotError:
        return PostgresSnapshotError(code, location=location, cause_type=cause_type)

    def open_connection(self, config: PostgresSnapshotConfig, spill_dir: Path) -> Any:
        return _open_connection(config, spill_dir)

    def load_extension(self, conn: Any, config: PostgresSnapshotConfig) -> None:
        _load_postgres_extension(conn, config)

    def pre_attach(self, conn: Any, config: PostgresSnapshotConfig) -> None:
        conn.execute(f"SET pg_statement_timeout_millis = {int(config.statement_timeout_s) * 1000}")

    def attach(self, conn: Any, config: PostgresSnapshotConfig, alias: str) -> None:
        conn.execute(
            f"ATTACH {_lit(config.dsn.get_secret_value())} AS {_q(alias)} "
            "(TYPE postgres, READ_ONLY)"
        )

    def check_types(self, conn: Any, sel: RelationSelection, alias: str) -> dict[str, str]:
        return _check_types(conn, sel)

    def guard_values(
        self, conn: Any, sel: RelationSelection, classes: dict[str, str], alias: str
    ) -> None:
        _guard_values(conn, sel, classes)

    def export_relation(
        self, conn: Any, sel: RelationSelection, target: Path, row_group_size: int, alias: str
    ) -> None:
        _export_relation(conn, sel, target, row_group_size)

    def declared_types(
        self, conn: Any, alias: str
    ) -> dict[tuple[str, str, str], tuple[str, str]]:
        """Declared format_type + typtype for every column of the attached DB,
        the browse-wide form of ``_check_types``' per-relation catalog read."""
        try:
            rows = conn.execute(
                f"SELECT nspname, relname, attname, fmt, typtype "
                f"FROM postgres_query({_lit(_ATTACH_ALIAS)}, ?) "
                "AS q(nspname, relname, attname, fmt, typtype)",
                [_CATALOG_SQL],
            ).fetchall()
        except Exception as exc:
            raise PostgresSnapshotError(
                "catalog_read_failed", cause_type=type(exc).__name__
            ) from None
        return {(r[0], r[1], r[2]): (r[3], r[4]) for r in rows}

    def column_verdict(self, declared_type: str, typtype: str) -> tuple[str, str]:
        """Postgres is preserve-or-reject: a guard class -> supported, else
        rejected. No lossy tier (a lossy type is simply not carried)."""
        cls = _classify(declared_type, typtype)
        if cls is None:
            return "rejected", f"unsupported_type:{declared_type}"
        return "supported", cls


def snapshot_tables_to_parquet(
    config: PostgresSnapshotConfig,
    selections: list[RelationSelection],
    dest_dir: Path,
) -> dict[str, Path]:
    """Stage every selection from ONE snapshot; return ``{schema.table: parquet_path}``.

    One ATTACH, one transaction across all selections, one ordered COPY per
    relation. A single-relation snapshot is this same call with one selection.
    On any failure, timeout, or interrupt nothing is published and every
    staged and spill file is removed.
    """
    return run_snapshot(config, selections, dest_dir, dialect=_PostgresDialect())

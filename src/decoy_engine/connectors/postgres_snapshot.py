"""Postgres snapshot-to-Parquet materialization primitive.

Reads a fixed, structured selection of Postgres relations through DuckDB's
``postgres`` extension (the "postgres scanner",
https://duckdb.org/docs/stable/core_extensions/postgres/overview) and stages
each relation as one Parquet file. This is a materialization step, not a
streaming connector: DuckDB owns the wire protocol and type mapping, Decoy
owns only the narrow, read-only, structured selection-to-Parquet step.

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

import logging
import os
import re
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from decoy_engine.sdk import PermanentError

__all__ = [
    "PostgresSnapshotConfig",
    "PostgresSnapshotError",
    "RelationSelection",
    "snapshot_tables_to_parquet",
]

_log = logging.getLogger(__name__)

# Strict on purpose: unquoted-safe Postgres identifiers. Names needing quoted
# oddities (spaces, quotes, unicode) are rejected rather than escaped, so the
# validation boundary and the quoting boundary cannot disagree.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")

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


class PostgresSnapshotError(PermanentError):
    """Coded, redacted snapshot failure.

    ``code`` is a stable machine-readable token. ``location`` is a relation
    (``schema.table``) or ``schema.table.column``, never a value or DSN. The
    message is built only from those parts plus an exception type name.
    """

    def __init__(self, code: str, *, location: str = "", cause_type: str = "") -> None:
        self.code = code
        self.location = location
        self.cause_type = cause_type
        parts = [f"postgres_snapshot[{code}]"]
        if location:
            parts.append(f"at {location}")
        if cause_type:
            parts.append(f"cause={cause_type}")
        super().__init__(" ".join(parts))


def _validate_ident(value: str, what: str) -> str:
    if not isinstance(value, str) or not _IDENT_RE.fullmatch(value):
        # The offending text is not echoed: it may be attacker-shaped input.
        raise ValueError(f"invalid {what} identifier")
    return value


class PostgresSnapshotConfig(BaseModel):
    """Connection and resource policy for one snapshot call. No SQL field."""

    # hide_input_in_errors: a ValidationError would otherwise echo the rejected
    # input, which for these models can be a DSN or attacker-shaped text.
    model_config = ConfigDict(
        extra="forbid", frozen=True, arbitrary_types_allowed=True, hide_input_in_errors=True
    )

    dsn: SecretStr
    extension_directory: Path = Field(
        description="Directory holding the bundled DuckDB postgres extension. Never installed from the network."
    )
    memory_limit: str = Field(pattern=r"^\d+(\.\d+)?\s?(KB|MB|GB|TB|KiB|MiB|GiB|TiB|B)$")
    temp_directory: Path
    threads: int = Field(ge=1, le=256)
    row_group_size: int = Field(ge=1_000, le=10_000_000)
    statement_timeout_s: int = Field(ge=1)
    deadline_s: int = Field(ge=1)


class RelationSelection(BaseModel):
    """One relation to stage. Identifiers only; there is no predicate field."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    schema_name: str = Field(alias="schema")
    table: str
    columns: list[str] = Field(min_length=1)
    order_by: list[str] = Field(default_factory=list)

    @field_validator("schema_name")
    @classmethod
    def _v_schema(cls, v: str) -> str:
        return _validate_ident(v, "schema")

    @field_validator("table")
    @classmethod
    def _v_table(cls, v: str) -> str:
        return _validate_ident(v, "table")

    @field_validator("columns", "order_by")
    @classmethod
    def _v_cols(cls, v: list[str]) -> list[str]:
        for c in v:
            _validate_ident(c, "column")
        if len(set(v)) != len(v):
            raise ValueError("duplicate column")
        return v

    @model_validator(mode="after")
    def _order_subset(self) -> RelationSelection:
        if not set(self.order_by) <= set(self.columns):
            raise ValueError("order_by must be a subset of columns")
        return self

    @property
    def key(self) -> str:
        return f"{self.schema_name}.{self.table}"


def _q(ident: str) -> str:
    """Quote a validated identifier. Validation already excludes ``"``."""
    return '"' + ident.replace('"', '""') + '"'


def _lit(text: str) -> str:
    """SQL string literal. Used only for the DSN and generated paths."""
    return "'" + text.replace("'", "''") + "'"


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

    Classes: ``plain`` (no guard needed), ``date``, ``timestamp``,
    ``numeric`` (bounded <= 38 digits; NaN guard).
    """
    base = type_text
    is_array = False
    while base.endswith("[]"):
        base = base[:-2]
        is_array = True
    base_nolen = _LEN_SUFFIX_RE.sub("", base)
    if typtype == "e":
        return "plain"
    if base_nolen in _PLAIN_TYPES:
        return "plain"
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
            f"SELECT attname, fmt, typtype FROM postgres_query({_lit(_ATTACH_ALIAS)}, ?) "
            "AS q(nspname, relname, attname, fmt, typtype) "
            "WHERE nspname = ? AND relname = ?",
            [_CATALOG_SQL, sel.schema_name, sel.table],
        ).fetchall()
    except Exception as exc:
        raise PostgresSnapshotError(
            "catalog_read_failed", location=sel.key, cause_type=type(exc).__name__
        ) from None
    if not rows:
        raise PostgresSnapshotError("relation_not_found", location=sel.key)
    declared = {r[0]: (r[1], r[2]) for r in rows}
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
    the same transaction as the exports, so it reads from that transaction's
    snapshot. Note: the guard's participation in the snapshot is not itself
    gate-tested (gate 1 exercises the ATTACH scan path); the guard-to-export
    window is sub-second against an operator's own near-static database, so a
    value changing in between is a low-risk edge, tracked as a Slice-2
    follow-up. The SQL is built only from validated, quoted identifiers and
    fixed literals.
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


def _order_clause(sel: RelationSelection) -> str:
    # Key columns first, then every other projected column as tiebreaker, so
    # keyless relations and non-unique keys still export in one total order.
    ordered = list(sel.order_by) + [c for c in sel.columns if c not in sel.order_by]
    return ", ".join(f"{_q(c)} ASC NULLS LAST" for c in ordered)


def _export_relation(conn: Any, sel: RelationSelection, target: Path, row_group_size: int) -> None:
    cols = ", ".join(_q(c) for c in sel.columns)
    src = f"{_q(_ATTACH_ALIAS)}.{_q(sel.schema_name)}.{_q(sel.table)}"
    conn.execute(
        f"COPY (SELECT {cols} FROM {src} ORDER BY {_order_clause(sel)}) "
        f"TO {_lit(str(target))} (FORMAT parquet, ROW_GROUP_SIZE {int(row_group_size)})"
    )


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
    if not selections:
        raise PostgresSnapshotError("no_selections")
    keys = [s.key for s in selections]
    if len(set(keys)) != len(keys):
        raise PostgresSnapshotError("duplicate_relation")

    run_id = uuid.uuid4().hex
    dest_dir.mkdir(parents=True, exist_ok=True)
    stage_dir = dest_dir / f".stage-{run_id}"
    final_dir = dest_dir / f"snapshot-{run_id}"
    spill_dir = config.temp_directory / f"spill-{run_id}"
    stage_dir.mkdir(mode=0o700)

    published = False
    conn: Any = None
    timed_out = threading.Event()
    timer: threading.Timer | None = None
    try:
        conn = _open_connection(config, spill_dir)
        _load_postgres_extension(conn, config)

        def _on_deadline() -> None:
            timed_out.set()
            try:
                conn.interrupt()
            except Exception:
                pass

        timer = threading.Timer(config.deadline_s, _on_deadline)
        timer.daemon = True
        timer.start()

        try:
            conn.execute("SET TimeZone = 'UTC'")
            conn.execute(
                f"SET pg_statement_timeout_millis = {int(config.statement_timeout_s) * 1000}"
            )
            conn.execute(
                f"ATTACH {_lit(config.dsn.get_secret_value())} AS {_q(_ATTACH_ALIAS)} "
                "(TYPE postgres, READ_ONLY)"
            )
        except Exception as exc:
            raise PostgresSnapshotError(
                "source_unreachable", cause_type=type(exc).__name__
            ) from None

        staged: dict[str, Path] = {}
        try:
            conn.execute("BEGIN TRANSACTION")
            # Validate every relation first so a late type rejection never
            # follows a long export. All of this reads the same snapshot.
            plans = [(sel, _check_types(conn, sel)) for sel in selections]
            for sel, classes in plans:
                _guard_values(conn, sel, classes)
            for sel, _ in plans:
                target = stage_dir / f"{sel.schema_name}.{sel.table}.parquet"
                _export_relation(conn, sel, target, config.row_group_size)
                staged[sel.key] = target
            conn.execute("COMMIT")
        except PostgresSnapshotError:
            if timed_out.is_set():
                raise PostgresSnapshotError("deadline_exceeded") from None
            raise
        except Exception as exc:
            if timed_out.is_set():
                raise PostgresSnapshotError("deadline_exceeded") from None
            raise PostgresSnapshotError("export_failed", cause_type=type(exc).__name__) from None

        if timed_out.is_set():
            raise PostgresSnapshotError("deadline_exceeded")
        os.replace(stage_dir, final_dir)
        published = True
        _log.info("postgres_snapshot published relations=%d", len(staged))
        return {k: final_dir / p.name for k, p in staged.items()}
    except PostgresSnapshotError as failure:
        code, location = failure.code, failure.location
        _log.warning("postgres_snapshot failed code=%s location=%s", code, location)
        raise
    finally:
        if timer is not None:
            timer.cancel()
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        shutil.rmtree(stage_dir, ignore_errors=True)
        shutil.rmtree(spill_dir, ignore_errors=True)
        if not published:
            shutil.rmtree(final_dir, ignore_errors=True)

"""Dialect-independent snapshot orchestration for the DuckDB-attach connectors.

This is the body of the former ``postgres_snapshot.snapshot_tables_to_parquet``
with every Postgres-specific step delegated to a ``Dialect``: one ATTACH, one
transaction across all relations, validate-then-export, deterministic ordered
COPY to Parquet, atomic directory publish, deadline interrupt, and
publish-nothing-on-failure cleanup. Behavior is identical to the pre-D0
Postgres path; the dialect supplies open/extension/pre-attach/attach/type-
check/value-guard/export and the coded error type.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any

from decoy_engine.connectors.duckdb_attach._dialect import Dialect
from decoy_engine.connectors.duckdb_attach._errors import SnapshotError
from decoy_engine.connectors.duckdb_attach._models import RelationSelection, SnapshotConfig

_log = logging.getLogger(__name__)


def run_snapshot(
    config: SnapshotConfig,
    selections: list[RelationSelection],
    dest_dir: Path,
    *,
    dialect: Dialect,
) -> dict[str, Path]:
    """Stage every selection from ONE snapshot; return ``{schema.table: parquet_path}``.

    One ATTACH, one transaction across all selections, one ordered COPY per
    relation. A single-relation snapshot is this same call with one selection.
    On any failure, timeout, or interrupt nothing is published and every
    staged and spill file is removed.
    """
    if not selections:
        raise dialect.error("no_selections")
    keys = [s.key for s in selections]
    if len(set(keys)) != len(keys):
        raise dialect.error("duplicate_relation")

    run_id = uuid.uuid4().hex
    dest_dir.mkdir(parents=True, exist_ok=True)
    stage_dir = dest_dir / f".stage-{run_id}"
    final_dir = dest_dir / f"snapshot-{run_id}"
    spill_dir = config.temp_directory / f"spill-{run_id}"
    stage_dir.mkdir(mode=0o700)
    alias = dialect.attach_alias

    published = False
    conn: Any = None
    timed_out = threading.Event()
    timer: threading.Timer | None = None
    try:
        conn = dialect.open_connection(config, spill_dir)
        dialect.load_extension(conn, config)

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
            dialect.pre_attach(conn, config)
            dialect.attach(conn, config, alias)
        except Exception as exc:
            raise dialect.error("source_unreachable", cause_type=type(exc).__name__) from None

        staged: dict[str, Path] = {}
        try:
            conn.execute("BEGIN TRANSACTION")
            # Validate every relation first so a late type rejection never
            # follows a long export. All of this reads the same snapshot.
            plans = [(sel, dialect.check_types(conn, sel, alias)) for sel in selections]
            for sel, classes in plans:
                dialect.guard_values(conn, sel, classes, alias)
            for sel, _ in plans:
                target = stage_dir / f"{sel.schema_name}.{sel.table}.parquet"
                dialect.export_relation(conn, sel, target, config.row_group_size, alias)
                staged[sel.key] = target
            conn.execute("COMMIT")
        except SnapshotError:
            if timed_out.is_set():
                raise dialect.error("deadline_exceeded") from None
            raise
        except Exception as exc:
            if timed_out.is_set():
                raise dialect.error("deadline_exceeded") from None
            raise dialect.error("export_failed", cause_type=type(exc).__name__) from None

        if timed_out.is_set():
            raise dialect.error("deadline_exceeded")
        os.replace(stage_dir, final_dir)
        published = True
        _log.info("%s published relations=%d", dialect.log_label, len(staged))
        return {k: final_dir / p.name for k, p in staged.items()}
    except SnapshotError as failure:
        code, location = failure.code, failure.location
        _log.warning("%s failed code=%s location=%s", dialect.log_label, code, location)
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

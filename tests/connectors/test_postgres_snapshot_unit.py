"""Postgres snapshot primitive: no-database suites (gates 2 and 3, leak)."""

from __future__ import annotations

import shutil
import traceback
from pathlib import Path

import pytest

pytest.importorskip("duckdb")

from pydantic import ValidationError

from decoy_engine.connectors import postgres_snapshot as ps
from decoy_engine.connectors.postgres_snapshot import (
    PostgresSnapshotConfig,
    PostgresSnapshotError,
    RelationSelection,
    snapshot_tables_to_parquet,
)

_INJECTIONS = [
    'x"; DROP TABLE users; --',
    "x'; select 1; --",
    "a b",
    "a.b",
    "",
    "1abc",
    "x" * 64,
    "x\x00y",
    "t) UNION SELECT 1 --",
]


def _sel(**over):
    base = {"schema": "public", "table": "t", "columns": ["a"], "order_by": []}
    base.update(over)
    return RelationSelection(**base)


# ----- GATE 2: structured-only / identifier safety -------------------------


@pytest.mark.parametrize("bad", _INJECTIONS)
def test_injection_rejected_in_schema(bad):
    with pytest.raises(ValidationError):
        _sel(schema=bad)


@pytest.mark.parametrize("bad", _INJECTIONS)
def test_injection_rejected_in_table(bad):
    with pytest.raises(ValidationError):
        _sel(table=bad)


@pytest.mark.parametrize("bad", _INJECTIONS)
def test_injection_rejected_in_column(bad):
    with pytest.raises(ValidationError):
        _sel(columns=["ok", bad])


@pytest.mark.parametrize("bad", _INJECTIONS)
def test_injection_rejected_in_order_by(bad):
    with pytest.raises(ValidationError):
        _sel(columns=["ok", bad], order_by=[bad])


def test_validation_error_does_not_echo_the_payload():
    payload = "zz_PAYLOAD_zz; drop"
    with pytest.raises(ValidationError) as ei:
        _sel(table=payload)
    assert "PAYLOAD" not in str(ei.value)


@pytest.mark.parametrize("field", ["where", "predicate", "sql", "query", "filter", "limit"])
def test_unknown_and_raw_sql_fields_rejected(field):
    with pytest.raises(ValidationError):
        _sel(**{field: "1=1"})


def test_no_raw_sql_or_predicate_field_exists():
    names = set(RelationSelection.model_fields) | set(PostgresSnapshotConfig.model_fields)
    assert not names & {"sql", "query", "where", "predicate", "filter", "raw"}


def test_columns_must_be_explicit_and_non_empty():
    with pytest.raises(ValidationError):
        _sel(columns=[])
    with pytest.raises(ValidationError):
        _sel(columns=["*"])


def test_order_by_must_be_projected_and_unique():
    with pytest.raises(ValidationError):
        _sel(columns=["a"], order_by=["b"])
    with pytest.raises(ValidationError):
        _sel(columns=["a", "a"])


def test_config_rejects_unknown_fields(tmp_path):
    with pytest.raises(ValidationError):
        PostgresSnapshotConfig(
            dsn="x",
            extension_directory=tmp_path,
            memory_limit="1GB",
            temp_directory=tmp_path,
            threads=1,
            row_group_size=1000,
            statement_timeout_s=1,
            deadline_s=1,
            sql="select 1",
        )


def test_quoting_doubles_embedded_quotes():
    # Validation already excludes quotes; the quoter must still be safe alone.
    assert ps._q('a"b') == '"a""b"'


def test_empty_and_duplicate_selection_rejected(tmp_path):
    cfg = PostgresSnapshotConfig(
        dsn="x",
        extension_directory=tmp_path,
        memory_limit="1GB",
        temp_directory=tmp_path / "s",
        threads=1,
        row_group_size=1000,
        statement_timeout_s=1,
        deadline_s=1,
    )
    with pytest.raises(PostgresSnapshotError) as e1:
        snapshot_tables_to_parquet(cfg, [], tmp_path / "d")
    assert e1.value.code == "no_selections"
    with pytest.raises(PostgresSnapshotError) as e2:
        snapshot_tables_to_parquet(cfg, [_sel(), _sel()], tmp_path / "d")
    assert e2.value.code == "duplicate_relation"


# ----- GATE 3: offline extension ------------------------------------------


def _cfg(ext: Path, tmp_path: Path, dsn="host=127.0.0.1 port=1 user=u password=PW_SECRET dbname=d"):
    return PostgresSnapshotConfig(
        dsn=dsn,
        extension_directory=ext,
        memory_limit="256MB",
        temp_directory=tmp_path / "spill",
        threads=1,
        row_group_size=1000,
        statement_timeout_s=5,
        deadline_s=20,
    )


def test_extension_loads_from_local_dir_with_autoinstall_disabled(ext_dir, tmp_path):
    cfg = _cfg(ext_dir, tmp_path)
    conn = ps._open_connection(cfg, tmp_path / "spill" / "x")
    try:
        assert (
            conn.execute("SELECT current_setting('autoinstall_known_extensions')").fetchone()[0]
            is False
        )
        assert (
            conn.execute("SELECT current_setting('autoload_known_extensions')").fetchone()[0]
            is False
        )
        ps._load_postgres_extension(conn, cfg)
    finally:
        conn.close()


def test_missing_extension_fails_closed_and_never_fetches(tmp_path):
    empty = tmp_path / "empty_ext"
    empty.mkdir()
    cfg = _cfg(empty, tmp_path)
    with pytest.raises(PostgresSnapshotError) as ei:
        snapshot_tables_to_parquet(cfg, [_sel()], tmp_path / "dest")
    # A network INSTALL would have succeeded and moved on to a connect error.
    assert ei.value.code == "extension_unavailable"
    assert list(empty.rglob("*.duckdb_extension")) == []


def test_incompatible_extension_binary_fails_closed(ext_dir, tmp_path):
    bad = tmp_path / "bad_ext"
    shutil.copytree(ext_dir, bad)
    files = list(bad.rglob("postgres_scanner.duckdb_extension"))
    assert files, "fixture layout changed"
    for f in files:
        f.write_bytes(b"not an extension binary" * 64)
    cfg = _cfg(bad, tmp_path)
    with pytest.raises(PostgresSnapshotError) as ei:
        snapshot_tables_to_parquet(cfg, [_sel()], tmp_path / "dest")
    assert ei.value.code == "extension_unavailable"


def test_failed_extension_load_leaves_no_staged_files(tmp_path):
    empty = tmp_path / "empty_ext"
    empty.mkdir()
    dest = tmp_path / "dest"
    with pytest.raises(PostgresSnapshotError):
        snapshot_tables_to_parquet(_cfg(empty, tmp_path), [_sel()], dest)
    assert list(dest.iterdir()) == []
    assert not (tmp_path / "spill").exists() or list((tmp_path / "spill").iterdir()) == []


# ----- Leak rule ------------------------------------------------------------


def test_connect_failure_leaks_no_dsn(ext_dir, tmp_path, caplog):
    caplog.set_level("DEBUG")
    cfg = _cfg(ext_dir, tmp_path)
    with pytest.raises(PostgresSnapshotError) as ei:
        snapshot_tables_to_parquet(cfg, [_sel()], tmp_path / "dest")
    err = ei.value
    assert err.code == "source_unreachable"
    rendered = "".join(traceback.format_exception(err)) + str(err) + repr(err)
    rendered += "\n".join(r.getMessage() for r in caplog.records)
    for needle in ("PW_SECRET", "127.0.0.1", "port=1", "user=u", "dbname"):
        assert needle not in rendered
    assert err.__cause__ is None


def test_dsn_secretstr_repr_hides_value(tmp_path):
    cfg = _cfg(tmp_path, tmp_path)
    assert "PW_SECRET" not in repr(cfg) and "PW_SECRET" not in str(cfg)
    assert "PW_SECRET" not in cfg.model_dump_json()

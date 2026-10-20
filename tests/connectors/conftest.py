"""Shared fixtures for the Postgres snapshot suites.

A throwaway Postgres runs in docker (or an operator supplies
DECOY_TEST_PG_DSN). The DuckDB postgres extension is installed once into a
local directory, the same shape as the bundled production directory, so the
tests exercise the offline load path rather than the network auto-install.
Source mutation goes through DuckDB's own postgres_execute, which keeps
psycopg out of the test dependencies.
"""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    exe = shutil.which("docker")
    assert exe is not None
    return subprocess.run([exe, *args], capture_output=True, text=True, check=False)  # noqa: S603


def _docker_postgres() -> tuple[str, str]:
    if shutil.which("docker") is None:
        pytest.skip("docker unavailable")
    started = _docker(
        "run", "-d", "--rm", "-e", "POSTGRES_PASSWORD=pw", "-p", "127.0.0.1::5432", "postgres:16"
    )
    if started.returncode != 0:
        pytest.skip("cannot start postgres container")
    cid = started.stdout.strip()
    port = _docker("port", cid, "5432/tcp").stdout.strip().splitlines()[0].rsplit(":", 1)[1]
    ready = ("exec", cid, "pg_isready", "-U", "postgres")
    for _ in range(60):
        # The image restarts once after init, so require two ready probes.
        if _docker(*ready).returncode == 0:
            time.sleep(1.5)
            if _docker(*ready).returncode == 0:
                break
        time.sleep(1)
    return cid, f"host=127.0.0.1 port={port} user=postgres password=pw dbname=postgres"


@pytest.fixture(scope="session")
def pg_dsn():
    import os

    env = os.environ.get("DECOY_TEST_PG_DSN")
    if env:
        yield env
        return
    cid, dsn = _docker_postgres()
    try:
        yield dsn
    finally:
        _docker("kill", cid)


@pytest.fixture(scope="session")
def ext_dir(tmp_path_factory):
    duckdb = pytest.importorskip("duckdb")
    """Local stand-in for the bundled extension directory."""
    d = tmp_path_factory.mktemp("duckdb_ext")
    c = duckdb.connect(config={"extension_directory": str(d)})
    try:
        c.execute("INSTALL postgres")
    except Exception:
        pytest.skip("postgres extension cannot be installed for the test fixture")
    finally:
        c.close()
    return d


class PgAdmin:
    """Runs DDL/DML on the source through DuckDB's postgres_execute."""

    def __init__(self, dsn: str, ext: Path):
        import duckdb

        self._c = duckdb.connect(config={"extension_directory": str(ext)})
        self._c.execute("LOAD postgres")
        self._c.execute(
            "ATTACH ? AS admin (TYPE postgres)".replace("?", "'" + dsn.replace("'", "''") + "'")
        )

    def run(self, sql: str) -> None:
        self._c.execute("CALL postgres_execute('admin', ?)", [sql])

    def close(self) -> None:
        self._c.close()


@pytest.fixture
def pg(pg_dsn, ext_dir):
    admin = PgAdmin(pg_dsn, ext_dir)
    schema = "t" + uuid.uuid4().hex[:10]
    admin.run(f"CREATE SCHEMA {schema}")
    admin.schema = schema  # type: ignore[attr-defined]
    admin.dsn = pg_dsn  # type: ignore[attr-defined]
    try:
        yield admin
    finally:
        admin.run(f"DROP SCHEMA {schema} CASCADE")
        admin.close()


@pytest.fixture
def make_config(pg_dsn, ext_dir, tmp_path):
    def _make(**over):
        from decoy_engine.connectors.postgres_snapshot import PostgresSnapshotConfig

        base = dict(
            dsn=pg_dsn,
            extension_directory=ext_dir,
            memory_limit="512MB",
            temp_directory=tmp_path / "spill",
            threads=4,
            row_group_size=100_000,
            statement_timeout_s=60,
            deadline_s=120,
        )
        base.update(over)
        return PostgresSnapshotConfig(**base)

    return _make

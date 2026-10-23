"""Offline extension bundling + setting-name drift (DuckDB sources D5c).

The plan's D5 acceptance: *extensions load only from the bundle with auto-install
off; a renamed setting fails the drift test rather than silently changing
behavior.* Two guarantees, two runnable checks:

1. **Offline fence is on.** Every DuckDB session the connectors open has
   auto-install, auto-load, and unsigned extensions disabled, so a stray
   autoload can never reach the network. Exercised against the shipped
   file-provider session (no extension, no network needed).

2. **Setting-name drift.** The connectors depend on specific DuckDB setting
   names (set either in the connect config or via ``SET``). A DuckDB upgrade that
   renamed or removed one would otherwise change behavior silently; this pins
   each name so the rename fails loudly instead.

3. **Bundle load path.** When the postgres scanner is installed into a pinned
   directory (the operator-bundle shape), it loads *from that directory* with
   auto-install off -- ``duckdb_extensions()`` reports its ``install_path`` under
   the bundle, never a network fetch. Skipped only when the extension cannot be
   installed in this environment (offline sandbox); CI installs it per target.

A MySQL filter-pushdown setting-name drift (``mysql_experimental_filter_pushdown``
-> ``mysql_enable_filter_pushdown``) is deferred with the MySQL adapter (D3): it
belongs with that dialect's fixture matrix, not here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("duckdb")

from decoy_engine.connectors.duckdb_attach import DuckDbFileDialect, SnapshotConfig

# Core DuckDB setting names the connectors set at connect/session time. Each is
# wired where the comment says; a rename/removal in a DuckDB upgrade must fail
# this test, not change behavior quietly.
_DEPENDED_SETTINGS = frozenset(
    {
        "temp_directory",  # open_connection config (spill dir)
        "memory_limit",  # open_connection config
        "threads",  # open_connection config
        "extension_directory",  # postgres open_connection config (bundle dir)
        "autoinstall_known_extensions",  # offline fence (all three)
        "autoload_known_extensions",
        "allow_unsigned_extensions",
        "preserve_insertion_order",  # deterministic row order through the session
        "TimeZone",  # _core/_catalog: SET TimeZone = 'UTC'
    }
)


def _config(tmp_path: Path) -> SnapshotConfig:
    return SnapshotConfig(
        dsn="/unused/for/connection-only",
        extension_directory=tmp_path / "ext",
        memory_limit="256MB",
        temp_directory=tmp_path / "spill",
        threads=1,
        row_group_size=1000,
        statement_timeout_s=5,
        deadline_s=20,
    )


def test_file_session_offline_fence_is_on(tmp_path: Path) -> None:
    conn = DuckDbFileDialect().open_connection(_config(tmp_path), tmp_path / "spill")
    try:
        autoinstall, autoload, unsigned = conn.execute(
            "SELECT current_setting('autoinstall_known_extensions'), "
            "current_setting('autoload_known_extensions'), "
            "current_setting('allow_unsigned_extensions')"
        ).fetchone()
    finally:
        conn.close()
    assert autoinstall is False
    assert autoload is False
    assert unsigned is False


def test_depended_setting_names_still_exist(tmp_path: Path) -> None:
    conn = DuckDbFileDialect().open_connection(_config(tmp_path), tmp_path / "spill")
    try:
        missing = []
        for name in sorted(_DEPENDED_SETTINGS):
            try:
                # current_setting resolves the name case-insensitively; a removed
                # or renamed setting raises instead of returning a value.
                conn.execute(f"SELECT current_setting('{name}')").fetchone()
            except Exception:
                missing.append(name)
    finally:
        conn.close()
    assert missing == [], f"DuckDB no longer recognizes these pinned settings: {missing}"


def test_postgres_scanner_loads_from_the_bundle_not_the_network(tmp_path: Path) -> None:
    import duckdb

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    # Build the operator bundle: INSTALL into a pinned directory (the one place a
    # network fetch is allowed, mirroring the deploy-time bundle build).
    installer = duckdb.connect(config={"extension_directory": str(bundle)})
    try:
        installer.execute("INSTALL postgres")
    except Exception:
        pytest.skip("postgres DuckDB extension cannot be installed in this environment")
    finally:
        installer.close()

    # A fresh session with the offline fence ON must still LOAD it -- from the
    # bundle, with no network install -- and report the bundle as its source.
    conn = duckdb.connect(
        config={
            "extension_directory": str(bundle),
            "autoinstall_known_extensions": False,
            "autoload_known_extensions": False,
            "allow_unsigned_extensions": False,
        }
    )
    try:
        conn.execute("LOAD postgres")
        rows = conn.execute(
            "SELECT extension_name, installed, install_path "
            "FROM duckdb_extensions() WHERE extension_name = 'postgres'"
        ).fetchall()
    finally:
        conn.close()

    assert rows, "postgres extension not reported by duckdb_extensions()"
    _name, installed, install_path = rows[0]
    assert installed is True
    # Loaded from the bundle directory, never a network/core autoload path.
    assert install_path, "install_path is empty -- extension did not load from the bundle"
    assert str(bundle) in str(install_path)

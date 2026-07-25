"""QA-2 (2026-05-31): _BANNED_RE denylist extension for DuckDB file-reading
table functions + quoted-path-FROM rejection.

Locks the contract that `data_discovery._validate_select_only` rejects:
- `read_csv`, `read_csv_auto`, `read_parquet`, `read_json`, `read_ndjson`
  function calls (arbitrary-file-read surface).
- `FROM '/path/to/file'` and `FROM "/path/to/file"` leading-quote shapes.

And does NOT false-positive on:
- A column literal named `read_csv` (SELECT read_csv AS r FROM v).

Plus a regression cell that the existing DDL/DML denylist still works.

Source: docs/audit/dennis-qa-triage-2026-05-31.md M20.
"""

from __future__ import annotations

import pytest

from decoy_engine.data_discovery import DiscoverySqlError, _validate_select_only


@pytest.mark.parametrize(
    "fn",
    ["read_csv", "read_csv_auto", "read_parquet", "read_json", "read_ndjson"],
)
def test_read_function_call_rejected(fn: str) -> None:
    """Every DuckDB file-read function name must be rejected. Pre-fix
    `SELECT * FROM read_csv('/etc/passwd')` passed the validator + was
    executed by DuckDB."""
    sql = f"SELECT * FROM {fn}('/etc/passwd')"
    with pytest.raises(DiscoverySqlError, match=fn.upper()):
        _validate_select_only(sql)


def test_quoted_path_in_from_clause_rejected_single_quote() -> None:
    """A single-quoted path in FROM must be rejected. DuckDB accepts
    this shape as an auto-detect table reference; the function-call
    denylist did not catch it."""
    with pytest.raises(DiscoverySqlError, match="quoted path"):
        _validate_select_only("SELECT * FROM '/etc/passwd'")


def test_quoted_path_in_from_clause_rejected_double_quote() -> None:
    """A double-quoted path in FROM must also be rejected."""
    with pytest.raises(DiscoverySqlError, match="quoted path"):
        _validate_select_only('SELECT * FROM "/etc/passwd"')


def test_column_alias_named_read_csv_conservatively_rejected() -> None:
    """A SELECT projection that aliases a column to the identifier
    `read_csv` is conservatively REJECTED by the denylist (the regex
    matches the function name without requiring a paren after).

    This is the documented false-positive cost of the cheap regex
    denylist (per the spec's pitfalls section). We pin the
    conservative behavior: better to reject + force the user to
    rename their alias than risk a function-call bypass."""
    sql = "SELECT user_id AS read_csv FROM staged_view"
    with pytest.raises(DiscoverySqlError):
        _validate_select_only(sql)


def test_existing_ddl_dml_denylist_still_works() -> None:
    """Regression: the existing DDL/DML keywords are still rejected
    after the denylist extension."""
    with pytest.raises(DiscoverySqlError, match="INSERT"):
        _validate_select_only("INSERT INTO t VALUES (1)")
    with pytest.raises(DiscoverySqlError, match="DROP"):
        _validate_select_only("DROP TABLE t")
    with pytest.raises(DiscoverySqlError, match="ATTACH"):
        _validate_select_only("ATTACH 'db.duckdb'")


def test_legitimate_select_against_staged_view_passes() -> None:
    """A simple SELECT against an identifier-named view is legitimate
    and must pass. The denylist extension does not break the happy
    path."""
    _validate_select_only("SELECT name, value FROM staged_view")
    _validate_select_only("SELECT COUNT(*) FROM staged_view WHERE active = true")


def test_legitimate_with_cte_passes() -> None:
    """WITH-leading CTEs are legitimate read-only constructs."""
    _validate_select_only("WITH t AS (SELECT * FROM staged_view) SELECT count(*) FROM t")

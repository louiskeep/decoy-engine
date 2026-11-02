"""Characterization of the exact SQL `StreamFkJoiner` issues per operation.

Records every statement a joiner sends through its connection (after the
connection's own setup) for each public operation, and pins the exact text by
sha256, captured before the query builders and EXPLAIN parsing moved into
`_stream_join_plan.py`. A whitespace-normalized copy of each statement is also
pinned so a failure shows which query changed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from decoy_engine.execution.out_of_core._stream_join import StreamFkJoiner

from ._ooc_fixtures import remap_edge_fixture, simple_edge_fixture


class _RecordingConn:
    """Delegates to the real DuckDB connection, logging each `execute` SQL."""

    def __init__(self, conn: Any, log: list[str]) -> None:
        self._conn = conn
        self._log = log

    def execute(self, sql: str, *args: Any) -> Any:
        self._log.append(sql)
        return self._conn.execute(sql, *args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def _record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture: Callable[[Path], Any],
    operation: Callable[[StreamFkJoiner], object],
) -> list[str]:
    fx = fixture(tmp_path / "fx")
    log: list[str] = []
    with StreamFkJoiner(
        edge=fx.edge,
        parent_relation=fx.parent_relation,
        child_key_types=fx.child_key_types,
        temp_dir=tmp_path / "join",
        remap_seeds=fx.remap_seeds,
        job_seed=fx.job_seed,
    ) as joiner:
        joiner.stage_keys(fx.child.to_batches())
        real_ensure = joiner._ensure_conn
        monkeypatch.setattr(joiner, "_ensure_conn", lambda: _RecordingConn(real_ensure(), log))
        operation(joiner)
    return log


def _drain_ordered(joiner: StreamFkJoiner) -> None:
    with joiner.run_ordered_join(64, run_bytes_cap=8 * 1024 * 1024, merge_fan_in=4) as rows:
        list(rows)


_OPERATIONS: dict[str, Callable[[StreamFkJoiner], object]] = {
    "total_orphans": lambda j: j.total_orphans(),
    "iter_join_rows": lambda j: list(j.iter_join_rows(64)),
    "explain_join": lambda j: j.explain_join(),
    "run_ordered_join": _drain_ordered,
}

_FIXTURES: dict[str, Callable[[Path], Any]] = {
    "simple": simple_edge_fixture,
    "remap": remap_edge_fixture,
}


def _digest(statements: list[str]) -> str:
    return hashlib.sha256("\x1e".join(statements).encode("utf-8")).hexdigest()


def _normalized(statements: list[str]) -> list[str]:
    return [" ".join(sql.split()) for sql in statements]


@pytest.mark.parametrize("fixture_name", list(_FIXTURES))
@pytest.mark.parametrize("operation", list(_OPERATIONS))
def test_issued_sql_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture_name: str, operation: str
) -> None:
    log = _record(tmp_path, monkeypatch, _FIXTURES[fixture_name], _OPERATIONS[operation])
    assert _normalized(log) == _NORMALIZED[operation]
    assert _digest(log) == _GOLDEN[(fixture_name, operation)]


_NORMALIZED: dict[str, list[str]] = {
    "explain_join": [
        'EXPLAIN (FORMAT JSON) SELECT c."__decoy_row_nr", CASE WHEN '
        'p."__decoy_fk_join_key" IS NULL THEN NULL ELSE TRUE END AS '
        '"__decoy_parent_match", p."__decoy_masked_key" AS "__decoy_parent_masked_0" '
        "FROM child_keys c LEFT JOIN parent_keys p ON c.__decoy_fk_join_key = "
        'p."__decoy_fk_join_key"',
        "SELECT current_setting('disabled_optimizers')",
    ],
    "iter_join_rows": [
        'SELECT c."__decoy_row_nr", c."__decoy_fk_join_key", c."__decoy_src_0", '
        'p."__decoy_fk_join_key" AS "__decoy_parent_match", p."__decoy_masked_key" '
        'AS "__decoy_parent_masked_0" FROM child_keys c LEFT JOIN parent_keys p ON '
        'c.__decoy_fk_join_key = p."__decoy_fk_join_key" ORDER BY '
        "c.__decoy_row_nr"
    ],
    "run_ordered_join": [
        'EXPLAIN (FORMAT JSON) SELECT c."__decoy_row_nr", CASE WHEN '
        'p."__decoy_fk_join_key" IS NULL THEN NULL ELSE TRUE END AS '
        '"__decoy_parent_match", p."__decoy_masked_key" AS '
        '"__decoy_parent_masked_0" FROM child_keys c LEFT JOIN parent_keys p ON '
        'c.__decoy_fk_join_key = p."__decoy_fk_join_key"',
        'SELECT c."__decoy_row_nr", CASE WHEN p."__decoy_fk_join_key" IS NULL '
        'THEN NULL ELSE TRUE END AS "__decoy_parent_match", '
        'p."__decoy_masked_key" AS "__decoy_parent_masked_0" FROM child_keys c '
        "LEFT JOIN parent_keys p ON c.__decoy_fk_join_key = "
        'p."__decoy_fk_join_key"',
    ],
    "total_orphans": [
        "SELECT count(*) FROM child_keys c LEFT JOIN parent_keys p ON "
        'c.__decoy_fk_join_key = p."__decoy_fk_join_key" WHERE '
        'c.__decoy_fk_join_key IS NOT NULL AND p."__decoy_fk_join_key" IS NULL'
    ],
}

_GOLDEN: dict[tuple[str, str], str] = {
    ("remap", "explain_join"): "863381ac9a5d63d5a1e2cb1ed46ebf36af8775e973d025a36261d75ee198647a",
    ("remap", "iter_join_rows"): "56d848e8cfef9bbf4a24f534c37598dc950545219b1769020f340cf4dafb2d9c",
    (
        "remap",
        "run_ordered_join",
    ): "50b17aedbfa6978c487332241549eb8002786e90023c0aea5c405ecf5fd413ab",
    ("remap", "total_orphans"): "bee71532f0fb07799c774cd3c1f56f5dd8b5621a631eec9523e6be686413c462",
    ("simple", "explain_join"): "863381ac9a5d63d5a1e2cb1ed46ebf36af8775e973d025a36261d75ee198647a",
    (
        "simple",
        "iter_join_rows",
    ): "56d848e8cfef9bbf4a24f534c37598dc950545219b1769020f340cf4dafb2d9c",
    (
        "simple",
        "run_ordered_join",
    ): "50b17aedbfa6978c487332241549eb8002786e90023c0aea5c405ecf5fd413ab",
    ("simple", "total_orphans"): "bee71532f0fb07799c774cd3c1f56f5dd8b5621a631eec9523e6be686413c462",
}

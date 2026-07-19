"""FIX 2 (root cause 2): `connect_duckdb`'s spill-friendly connection config.

`preserve_insertion_order=False` lets DuckDB's window/sort/aggregate
operators spill instead of pinning state for order-preservation bookkeeping
the out-of-core route never needs on an unread connection (every caller that
DOES need order applies an explicit `ORDER BY` at the read site). `threads`
is derived from `memory_limit` (~2 GiB/thread) so an unthrottled thread count
cannot multiply per-thread overhead past a small cap before any operator even
spills.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from decoy_engine.execution.out_of_core._duckdb import (
    _threads_for_memory_limit,
    connect_duckdb,
)


def _setting(conn, name: str):
    return conn.execute(f"SELECT current_setting('{name}')").fetchone()[0]


class TestPreserveInsertionOrder:
    def test_always_disabled_regardless_of_memory_limit(self, tmp_path: Path) -> None:
        for memory_limit in (None, "128MB"):
            conn = connect_duckdb(temp_dir=tmp_path / f"d{memory_limit}", memory_limit=memory_limit)
            try:
                assert _setting(conn, "preserve_insertion_order") is False
            finally:
                conn.close()


class TestThreadsForMemoryLimit:
    def test_no_memory_limit_leaves_threads_unset(self, tmp_path: Path) -> None:
        # No memory_limit means no basis to derive threads from; DuckDB's own
        # default (the CPU count) stays in place rather than a guess.
        import os

        conn = connect_duckdb(temp_dir=tmp_path)
        try:
            assert _setting(conn, "threads") == (os.cpu_count() or 1)
        finally:
            conn.close()

    def test_threads_scale_down_for_a_small_memory_limit(self, tmp_path: Path) -> None:
        # 128 MiB budget: floor(128 MiB / 2 GiB) == 0, floored to 1 thread.
        conn = connect_duckdb(temp_dir=tmp_path, memory_limit="128MB")
        try:
            assert _setting(conn, "threads") == 1
        finally:
            conn.close()

    def test_threads_scale_up_with_memory_limit_but_cap_at_cpu_count(self, tmp_path: Path) -> None:
        import os

        # An enormous memory_limit must not exceed the host's CPU count.
        conn = connect_duckdb(temp_dir=tmp_path, memory_limit="1048576MB")  # 1 TiB
        try:
            assert _setting(conn, "threads") == (os.cpu_count() or 1)
        finally:
            conn.close()

    @pytest.mark.parametrize(
        ("memory_limit_mb", "expected_threads"),
        [
            (1, 1),  # far below one thread's 2 GiB share
            (2048, 1),  # exactly one thread's share
            (4096, 2),  # exactly two threads' share
        ],
    )
    def test_threads_formula_matches_two_gib_per_thread(
        self, memory_limit_mb: int, expected_threads: int
    ) -> None:
        import os

        threads = _threads_for_memory_limit(f"{memory_limit_mb}MB")
        assert threads == max(1, min(os.cpu_count() or 1, expected_threads))

    def test_unparseable_memory_limit_string_yields_no_override(self) -> None:
        # A memory_limit shape outside _budget.py's "<MiB>MB" contract (e.g. a
        # hand-written "1GB") must degrade to no threads override rather than
        # silently mis-deriving one.
        assert _threads_for_memory_limit("1GB") is None
        assert _threads_for_memory_limit("512mb") is None  # lowercase suffix

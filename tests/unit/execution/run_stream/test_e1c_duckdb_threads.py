"""RUN-STREAM E1c: `out_of_core_threads` is bound once per run and read at `connect_duckdb`.

Every nested DuckDB connection of the out-of-core route sees
`max(1, min(cap or detect_usable_cpus(), memory_limit // 2 GiB))`; the applied
`(memory_limit_bytes, threads)` pairs come from what each connection was opened with.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

import pytest

from decoy_engine.execution import ExecutionError, run_pipeline
from decoy_engine.execution.out_of_core import _duckdb
from decoy_engine.fileaccess.adapters import duckdb as duckdb_adapter
from tests.unit.execution.test_out_of_core_routing import _fk_ooc_config, _sources

_GIB = 1024**3
_BUDGET = 64 * _GIB


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every config handed to DuckDB's connect, in order."""
    configs: list[dict[str, Any]] = []
    real = duckdb_adapter.connect_internal

    def spy(config: dict[str, Any]) -> Any:
        configs.append(dict(config))
        return real(config)

    monkeypatch.setattr(duckdb_adapter, "connect_internal", spy)
    return configs


def _mem_bytes(config: dict[str, Any]) -> int:
    match = re.fullmatch(r"(\d+)MB", config["memory_limit"])
    assert match is not None
    return int(match.group(1)) * 1024 * 1024


def _expected_threads(config: dict[str, Any], *, cap: int | None, cpus: int) -> int:
    return max(1, min(cap or cpus, _mem_bytes(config) // (2 * _GIB)))


def _run(tmp_path: Path, **extra: Any) -> Any:
    config = _fk_ooc_config(tmp_path)
    return run_pipeline(
        config,
        _sources(config),
        engine_version="0.1.0",
        execution_mode="out_of_core",
        out_of_core_budget_bytes=_BUDGET,
        **extra,
    )


def test_every_nested_connection_sees_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(_duckdb, "detect_usable_cpus", lambda: 8)
    _run(tmp_path, out_of_core_threads=2)
    assert len(opened) >= 2  # the relation build and at least one joiner
    for config in opened:
        assert config["threads"] == _expected_threads(config, cap=2, cpus=8) == 2


def test_no_cap_keeps_no_user_cap_but_honors_detected_cpus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(_duckdb, "detect_usable_cpus", lambda: 3)
    _run(tmp_path)
    assert opened
    for config in opened:
        assert config["threads"] == _expected_threads(config, cap=None, cpus=3) == 3


def test_a_cap_above_the_detected_cpus_is_clamped_to_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(_duckdb, "detect_usable_cpus", lambda: 4)
    _run(tmp_path, out_of_core_threads=64)
    assert {c["threads"] for c in opened} == {4}


def test_a_small_memory_limit_takes_threads_below_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(_duckdb, "detect_usable_cpus", lambda: 8)
    config = _fk_ooc_config(tmp_path)
    result = run_pipeline(
        config,
        _sources(config),
        engine_version="0.1.0",
        execution_mode="out_of_core",
        out_of_core_budget_bytes=512 * 1024 * 1024,
        out_of_core_threads=6,
    )
    assert {c["threads"] for c in opened} == {1}  # 512 MiB // 2 GiB floors to one thread
    block = result.quality_metrics["resources"]["out_of_core"]
    assert block["thread_cap"] == 6
    assert {pair["threads"] for pair in block["applied"]} == {1}  # recorded, not inferred


def test_applied_pairs_are_the_distinct_connection_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(_duckdb, "detect_usable_cpus", lambda: 8)
    result = _run(tmp_path, out_of_core_threads=2)
    block = result.quality_metrics["resources"]["out_of_core"]
    assert block["thread_cap"] == 2
    want = sorted({(_mem_bytes(c), c["threads"]) for c in opened})
    got = [(pair["memory_limit_bytes"], pair["threads"]) for pair in block["applied"]]
    assert got == want


def test_a_run_without_a_thread_cap_still_records_the_route_resources(tmp_path: Path) -> None:
    # E3 supersedes "no cap keeps today's metrics": the route summary needs the budget and the
    # applied limits of every out-of-core run. The block only gains fields.
    result = _run(tmp_path)
    block = result.quality_metrics["resources"]["out_of_core"]
    assert block["thread_cap"] is None
    assert block["memory_cap_bytes"] == _BUDGET
    assert block["applied"]


def test_a_run_without_the_route_has_no_out_of_core_block(tmp_path: Path) -> None:
    config = _fk_ooc_config(tmp_path)
    result = run_pipeline(
        config, _sources(config), engine_version="0.1.0", execution_mode="sequential"
    )
    assert "out_of_core" not in result.quality_metrics.get("resources", {})


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "2"])
def test_invalid_cap_is_refused_before_any_work(tmp_path: Path, bad: Any) -> None:
    config = _fk_ooc_config(tmp_path)
    with pytest.raises(ExecutionError, match="out_of_core_threads") as raised:
        run_pipeline(config, _sources(config), engine_version="0.1.0", out_of_core_threads=bad)
    assert raised.value.code == "invalid_execution_knob"


def test_two_concurrent_run_contexts_do_not_bleed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_duckdb, "detect_usable_cpus", lambda: 16)
    barrier = threading.Barrier(2)
    seen: dict[int, tuple[list[int | None], list[tuple[int | None, int | None]]]] = {}

    def worker(cap: int) -> None:
        with _duckdb.bind_duckdb_run(cap) as recorder:
            barrier.wait(timeout=30)  # both contexts are live at once
            threads: list[int | None] = []
            for index in range(3):
                conn = _duckdb.connect_duckdb(
                    temp_dir=tmp_path / f"c{cap}-{index}", memory_limit="65536MB"
                )
                threads.append(conn.execute("SELECT current_setting('threads')").fetchone()[0])
                conn.close()
                barrier.wait(timeout=30)
            seen[cap] = (threads, [(p.memory_limit_bytes, p.threads) for p in recorder.applied()])

    pool = [threading.Thread(target=worker, args=(cap,)) for cap in (2, 5)]
    for t in pool:
        t.start()
    for t in pool:
        t.join(timeout=60)
    assert seen[2][0] == [2, 2, 2]
    assert seen[5][0] == [5, 5, 5]
    assert seen[2][1] == [(65536 * 1024 * 1024, 2)]
    assert seen[5][1] == [(65536 * 1024 * 1024, 5)]


def test_the_binding_ends_with_the_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_duckdb, "detect_usable_cpus", lambda: 16)
    with _duckdb.bind_duckdb_run(2):
        pass
    conn = _duckdb.connect_duckdb(temp_dir=tmp_path / "after", memory_limit="65536MB")
    try:
        assert conn.execute("SELECT current_setting('threads')").fetchone()[0] == 16
    finally:
        conn.close()

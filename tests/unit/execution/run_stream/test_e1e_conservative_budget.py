"""RUN-STREAM E1e: the engine-owned conservative budget for CLI `auto` memory.

`resolve_conservative_ooc_budget_bytes()` delegates to `resolve_budget(None)` (25% of the effective
ceiling). The out-of-core default for every other caller, ceiling minus the reserve, is unchanged.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

import decoy_engine
from decoy_engine.execution import out_of_core as ooc
from decoy_engine.execution import run_pipeline
from decoy_engine.execution.out_of_core import _budget
from tests.unit.execution.test_out_of_core_routing import _fk_ooc_config, _sources

_GIB = 1024**3
_HOST = 32 * _GIB


@pytest.fixture(autouse=True)
def host_32_gib(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_budget, "detect_effective_memory_bytes", lambda: _HOST)


def test_policy_is_a_quarter_of_the_effective_ceiling() -> None:
    assert _budget.resolve_conservative_ooc_budget_bytes() == 8 * _GIB
    assert decoy_engine.resolve_conservative_ooc_budget_bytes() == 8 * _GIB
    assert "resolve_conservative_ooc_budget_bytes" in decoy_engine.__all__


def test_policy_delegates_to_resolve_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    real = _budget.resolve_budget

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(_budget, "resolve_budget", spy)
    assert _budget.resolve_conservative_ooc_budget_bytes() == 8 * _GIB
    assert calls == [((None,), {})]


def test_library_default_for_none_is_unchanged() -> None:
    reserve = max(int(_HOST * 0.2), 2 * _GIB)
    assert _budget.resolve_ooc_memory_limit(None).budget_bytes == _HOST - reserve
    assert _budget.resolve_budget(None).budget_bytes == 8 * _GIB


def test_a_library_run_with_none_still_gets_the_ceiling_minus_reserve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[int | None] = []
    real = ooc.run_fk_out_of_core

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs["budget_bytes"])
        return real(*args, **kwargs)

    monkeypatch.setattr(ooc, "run_fk_out_of_core", spy)
    config = _fk_ooc_config(tmp_path)
    run_pipeline(
        config,
        _sources(config),
        engine_version="0.1.0",
        execution_mode="out_of_core",
        out_of_core_budget_bytes=None,
    )
    assert seen == [_HOST - max(int(_HOST * 0.2), 2 * _GIB)]


def test_two_concurrent_automatic_runs_each_get_an_explicit_quarter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    barrier = threading.Barrier(2)
    seen: dict[int, list[int | None]] = {1: [], 2: []}
    real = ooc.run_fk_out_of_core
    local = threading.local()

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen[local.run].append(kwargs["budget_bytes"])
        return real(*args, **kwargs)

    monkeypatch.setattr(ooc, "run_fk_out_of_core", spy)
    results: dict[int, Any] = {}

    def auto_run(index: int) -> None:
        local.run = index
        config = _fk_ooc_config(tmp_path / f"r{index}")
        barrier.wait(timeout=30)
        results[index] = run_pipeline(
            config,
            _sources(config),
            engine_version="0.1.0",
            execution_mode="out_of_core",
            out_of_core_budget_bytes=_budget.resolve_conservative_ooc_budget_bytes(),
            out_of_core_threads=index,
        )

    for index in (1, 2):
        (tmp_path / f"r{index}").mkdir()
    pool = [threading.Thread(target=auto_run, args=(i,)) for i in (1, 2)]
    for t in pool:
        t.start()
    for t in pool:
        t.join(timeout=120)
    assert seen == {1: [8 * _GIB], 2: [8 * _GIB]}
    caps = {i: results[i].quality_metrics["resources"]["out_of_core"]["thread_cap"] for i in (1, 2)}
    assert caps == {1: 1, 2: 2}  # neither run saw the other's state

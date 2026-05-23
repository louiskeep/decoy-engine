"""Memory-pressure warning emitted by run_graph when peak RSS crosses
the configurable threshold. Bug 5 follow-up.

Mocks psutil so the test is deterministic — we control "system memory"
and "process memory" separately, then verify the right warning fires
through the ctx.logger plumbing.
"""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from decoy_engine.graph.memory_monitor import check_memory_pressure as _check_memory_pressure


class _CapturingLogger:
    """Minimal Logger Protocol stand-in — records (level, msg, args)
    tuples so tests assert on what was logged."""

    def __init__(self) -> None:
        self.records: list[tuple[str, str, tuple]] = []

    def info(self, msg: str, *args) -> None:
        self.records.append(("info", msg, args))

    def warning(self, msg: str, *args) -> None:
        self.records.append(("warning", msg, args))

    def error(self, msg: str, *args) -> None:
        self.records.append(("error", msg, args))

    def debug(self, msg: str, *args) -> None:
        self.records.append(("debug", msg, args))


def _set_total_ram(gb: float):
    """Patch psutil.virtual_memory().total to a fixed value."""
    class _Vm:
        total = int(gb * 1024 * 1024 * 1024)
    return patch("psutil.virtual_memory", lambda: _Vm())


def test_no_warning_when_well_under_threshold():
    log = _CapturingLogger()
    # 1 GB peak on a 32 GB box → 3% — silent.
    with _set_total_ram(32):
        _check_memory_pressure(
            peak_rss_bytes=1 * 1024 * 1024 * 1024,
            graph_engine_mode="hybrid",
            log=log,
        )
    assert log.records == []


def test_hybrid_warning_above_threshold_suggests_pandas_override():
    log = _CapturingLogger()
    # 24 GB peak on a 32 GB box → 75% — fires (default threshold 70%).
    with _set_total_ram(32):
        _check_memory_pressure(
            peak_rss_bytes=24 * 1024 * 1024 * 1024,
            graph_engine_mode="hybrid",
            log=log,
        )
    assert len(log.records) == 1
    level, msg, _args = log.records[0]
    assert level == "warning"
    # The hybrid-specific advisory points at the override path.
    assert "engine: pandas" in msg
    assert "SHARED_ENGINE_ARCHITECTURE.md" in msg


def test_pandas_warning_above_threshold_does_not_suggest_override():
    log = _CapturingLogger()
    with _set_total_ram(32):
        _check_memory_pressure(
            peak_rss_bytes=24 * 1024 * 1024 * 1024,
            graph_engine_mode="pandas",
            log=log,
        )
    assert len(log.records) == 1
    level, msg, _args = log.records[0]
    assert level == "warning"
    # Pandas pipelines are already on the lower-memory path; the only
    # real recovery is more RAM.
    assert "engine: pandas" not in msg
    assert "larger instance" in msg


def test_no_warning_when_log_is_none():
    """Logger may be None when run_graph is called without a context.
    The check should silently no-op."""
    with _set_total_ram(32):
        _check_memory_pressure(
            peak_rss_bytes=24 * 1024 * 1024 * 1024,
            graph_engine_mode="hybrid",
            log=None,
        )
    # No exception raised = pass.


def test_threshold_overridable_via_env(monkeypatch):
    """Customers on quiet-or-loud preferences can re-tune the threshold
    at startup via DECOY_MEMORY_WARN_THRESHOLD. We re-import the module
    to pick up the new value because the constant captures the env var
    at import time.

    Updated 2026-05-23 (V2.0-A.4): the MEMORY_WARN_THRESHOLD constant
    moved from runner.py to memory_monitor.py along with the
    check_memory_pressure function. Reloading memory_monitor refreshes
    both the threshold and the function that reads it.
    """
    import importlib
    monkeypatch.setenv("DECOY_MEMORY_WARN_THRESHOLD", "0.9")

    import decoy_engine.graph.memory_monitor as mm_mod
    importlib.reload(mm_mod)

    log = _CapturingLogger()
    # 24 GB / 32 GB = 75% (under the new 90% threshold; no warning).
    with _set_total_ram(32):
        mm_mod.check_memory_pressure(
            peak_rss_bytes=24 * 1024 * 1024 * 1024,
            graph_engine_mode="hybrid",
            log=log,
        )
    assert log.records == []

    # 30 GB / 32 GB = ~94% (above the threshold; fires).
    with _set_total_ram(32):
        mm_mod.check_memory_pressure(
            peak_rss_bytes=30 * 1024 * 1024 * 1024,
            graph_engine_mode="hybrid",
            log=log,
        )
    assert len(log.records) == 1

    # Restore default threshold for other tests.
    monkeypatch.delenv("DECOY_MEMORY_WARN_THRESHOLD")
    importlib.reload(mm_mod)

"""Unit tests for decoy_engine.internal.memory.MemoryMonitor.

Coverage-audit task (audit/engine-coverage): memory.py was at 0% coverage.
These tests pin the observable contract of all three staticmethods across
every branch: the psutil-healthy path (with EXACT arithmetic asserted against
a controlled fake), the ImportError fallback, and the generic-Exception
fallback. psutil is imported lazily inside each method, so we drive the three
branches by monkeypatching the `psutil` entry in sys.modules:

- healthy: install a fake psutil module with known rss / total / percent;
- ImportError: set sys.modules['psutil'] = None so `import psutil` raises;
- generic Exception: install a fake whose Process()/virtual_memory() raises.

The logger contract is checked with a spy logger that records (level, message)
so we assert both the returned value AND that the right level fired with the
right substring.
"""

from __future__ import annotations

import sys
import types

import pytest

from decoy_engine.internal.memory import MemoryMonitor

# Known, controlled values so all arithmetic is EXACT, not "not None".
FAKE_RSS_BYTES = 512 * 1024 * 1024  # 512 MiB in bytes
EXPECTED_MB = FAKE_RSS_BYTES / (1024 * 1024)  # -> 512.0
FAKE_TOTAL_BYTES = 16 * 1024**3  # 16 GiB in bytes
EXPECTED_TOTAL_GB = FAKE_TOTAL_BYTES / (1024**3)  # -> 16.0
FAKE_PERCENT = 42.5


class SpyLogger:
    """Records logger calls as (level, message) tuples for assertion."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def info(self, msg: str) -> None:
        self.calls.append(("info", msg))

    def debug(self, msg: str) -> None:
        self.calls.append(("debug", msg))

    def warning(self, msg: str) -> None:
        self.calls.append(("warning", msg))

    def messages(self, level: str) -> list[str]:
        return [m for lvl, m in self.calls if lvl == level]


def _make_fake_psutil(
    *,
    rss: int = FAKE_RSS_BYTES,
    total: int = FAKE_TOTAL_BYTES,
    percent: float = FAKE_PERCENT,
    process_raises: Exception | None = None,
    vmem_raises: Exception | None = None,
) -> types.ModuleType:
    """Build a stand-in `psutil` module with controlled return values.

    Optionally make Process() or virtual_memory() raise, to exercise the
    generic-Exception fallback branch.
    """
    mod = types.ModuleType("psutil")

    class _MemInfo:
        def __init__(self, rss_bytes: int) -> None:
            self.rss = rss_bytes

    class _VMem:
        def __init__(self, total_bytes: int, pct: float) -> None:
            self.total = total_bytes
            self.percent = pct

    class _Process:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def memory_info(self) -> _MemInfo:
            return _MemInfo(rss)

    def _process_factory(pid: int) -> _Process:
        if process_raises is not None:
            raise process_raises
        return _Process(pid)

    def _virtual_memory() -> _VMem:
        if vmem_raises is not None:
            raise vmem_raises
        return _VMem(total, percent)

    mod.Process = _process_factory  # type: ignore[attr-defined]
    mod.virtual_memory = _virtual_memory  # type: ignore[attr-defined]
    return mod


@pytest.fixture
def fake_psutil(monkeypatch):
    """Install a healthy fake psutil; return it so a test can tweak it."""
    mod = _make_fake_psutil()
    monkeypatch.setitem(sys.modules, "psutil", mod)
    return mod


@pytest.fixture
def no_psutil(monkeypatch):
    """Force `import psutil` to raise ImportError inside the methods.

    Setting the sys.modules entry to None makes the import machinery raise
    ImportError on `import psutil` (documented CPython behavior)."""
    monkeypatch.setitem(sys.modules, "psutil", None)


# ---------------------------------------------------------------------------
# monitor_memory_usage
# ---------------------------------------------------------------------------


class TestMonitorMemoryUsage:
    def test_healthy_returns_exact_mb_and_logs(self, fake_psutil):
        logger = SpyLogger()
        result = MemoryMonitor.monitor_memory_usage(logger, label="Peak")

        assert result == EXPECTED_MB  # exact: 512.0

        info_msgs = logger.messages("info")
        assert len(info_msgs) == 1
        # Label threaded through and MB formatted to 2 dp.
        assert "Peak" in info_msgs[0]
        assert "512.00 MB" in info_msgs[0]

        # System line goes to debug with percent + GB.
        debug_msgs = logger.messages("debug")
        assert any("42.5%" in m and "16.0 GB" in m for m in debug_msgs)

        assert logger.messages("warning") == []

    def test_default_label_is_current(self, fake_psutil):
        logger = SpyLogger()
        MemoryMonitor.monitor_memory_usage(logger)
        assert "Current" in logger.messages("info")[0]

    def test_import_error_returns_none_and_warns(self, no_psutil):
        logger = SpyLogger()
        result = MemoryMonitor.monitor_memory_usage(logger)

        assert result is None
        warnings = logger.messages("warning")
        assert len(warnings) == 1
        assert "psutil module not available" in warnings[0]
        # Install hint on the debug channel.
        assert any("pip install psutil" in m for m in logger.messages("debug"))
        # The happy-path info line must NOT have fired.
        assert logger.messages("info") == []

    def test_generic_exception_returns_none_and_warns(self, monkeypatch):
        boom = RuntimeError("psutil exploded")
        mod = _make_fake_psutil(process_raises=boom)
        monkeypatch.setitem(sys.modules, "psutil", mod)

        logger = SpyLogger()
        result = MemoryMonitor.monitor_memory_usage(logger)

        assert result is None
        warnings = logger.messages("warning")
        assert len(warnings) == 1
        assert "Could not monitor memory: psutil exploded" in warnings[0]
        # Distinguishable from the ImportError message.
        assert "not available" not in warnings[0]

    def test_generic_exception_from_virtual_memory(self, monkeypatch):
        """Exception raised after the info-log line still yields None.

        virtual_memory() raises, so the info line fired but the method
        must still fall through to None (nothing returned early)."""
        boom = ValueError("vmem down")
        mod = _make_fake_psutil(vmem_raises=boom)
        monkeypatch.setitem(sys.modules, "psutil", mod)

        logger = SpyLogger()
        result = MemoryMonitor.monitor_memory_usage(logger)

        assert result is None
        assert any("Could not monitor memory: vmem down" in m for m in logger.messages("warning"))


# ---------------------------------------------------------------------------
# get_memory_usage
# ---------------------------------------------------------------------------


class TestGetMemoryUsage:
    def test_healthy_returns_exact_dict(self, fake_psutil):
        result = MemoryMonitor.get_memory_usage()
        assert result == {
            "process_memory_mb": EXPECTED_MB,  # 512.0
            "system_total_gb": EXPECTED_TOTAL_GB,  # 16.0
            "system_used_percent": FAKE_PERCENT,  # 42.5
        }

    def test_exact_keys(self, fake_psutil):
        result = MemoryMonitor.get_memory_usage()
        assert set(result.keys()) == {
            "process_memory_mb",
            "system_total_gb",
            "system_used_percent",
        }

    def test_import_error_returns_none_no_logging(self, no_psutil):
        # No logger argument exists; contract is simply None on ImportError.
        assert MemoryMonitor.get_memory_usage() is None

    def test_generic_exception_returns_none(self, monkeypatch):
        mod = _make_fake_psutil(process_raises=RuntimeError("boom"))
        monkeypatch.setitem(sys.modules, "psutil", mod)
        assert MemoryMonitor.get_memory_usage() is None


# ---------------------------------------------------------------------------
# is_memory_critical
# ---------------------------------------------------------------------------


class TestIsMemoryCritical:
    def test_above_threshold_is_critical(self, monkeypatch):
        mod = _make_fake_psutil(percent=90.1)
        monkeypatch.setitem(sys.modules, "psutil", mod)
        is_critical, pct = MemoryMonitor.is_memory_critical(threshold_percent=90.0)
        assert is_critical is True
        assert pct == 90.1

    def test_equal_threshold_is_not_critical(self, monkeypatch):
        """`>` is strict: used == threshold is NOT critical."""
        mod = _make_fake_psutil(percent=90.0)
        monkeypatch.setitem(sys.modules, "psutil", mod)
        is_critical, pct = MemoryMonitor.is_memory_critical(threshold_percent=90.0)
        assert is_critical is False
        assert pct == 90.0

    def test_below_threshold_is_not_critical(self, monkeypatch):
        mod = _make_fake_psutil(percent=89.9)
        monkeypatch.setitem(sys.modules, "psutil", mod)
        is_critical, pct = MemoryMonitor.is_memory_critical(threshold_percent=90.0)
        assert is_critical is False
        assert pct == 89.9

    def test_default_threshold_is_90(self, monkeypatch):
        mod = _make_fake_psutil(percent=95.0)
        monkeypatch.setitem(sys.modules, "psutil", mod)
        # No explicit threshold -> default 90.0, so 95 is critical.
        assert MemoryMonitor.is_memory_critical() == (True, 95.0)

    def test_custom_threshold_respected(self, monkeypatch):
        mod = _make_fake_psutil(percent=50.0)
        monkeypatch.setitem(sys.modules, "psutil", mod)
        # 50 > 40 -> critical under a lower threshold.
        assert MemoryMonitor.is_memory_critical(threshold_percent=40.0) == (True, 50.0)

    def test_import_error_returns_false_none(self, no_psutil):
        assert MemoryMonitor.is_memory_critical() == (False, None)

    def test_generic_exception_returns_false_none(self, monkeypatch):
        mod = _make_fake_psutil(vmem_raises=RuntimeError("boom"))
        monkeypatch.setitem(sys.modules, "psutil", mod)
        assert MemoryMonitor.is_memory_critical() == (False, None)

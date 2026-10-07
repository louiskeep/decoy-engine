"""Memory evidence for isolated runs: sampler, flag rule and wire format.

Plan: docs/plans/2026-10-07-oom-vmdata-monitor.md (tests 1, 3, 5, 7b). The flag is a
suspicion recorded next to an unchanged outcome, so these tests pin both halves: the
predicate with an injected clock, and `classify_abnormal_exit` as a table.
"""

from __future__ import annotations

import json
import signal
import threading
import time
from typing import Any

import pytest

from decoy_engine.execution import _isolated_memwatch as mw
from decoy_engine.execution._isolated_common import IsolatedRunResult, classify_abnormal_exit

_MIB = 1024 * 1024


class _Clock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _ScriptedReader:
    """Returns scripted field dicts, then raises like a vanished process."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[tuple[int, tuple[str, ...]]] = []

    def __call__(self, pid: int, names: tuple[str, ...]) -> dict[str, int]:
        self.calls.append((pid, names))
        if not self.script:
            raise FileNotFoundError(pid)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _sampler(script, *, kind="data", cap=1024 * _MIB, clock=None, **kw) -> mw.MemorySampler:
    return mw.MemorySampler(
        4242,
        cap,
        kind,
        read=_ScriptedReader(script),
        clock=clock or _Clock(),
        **kw,
    )


# ---------------------------------------------------------------- sampler units


class TestSamplerRecords:
    def test_last_max_and_count(self):
        clock = _Clock(10.0)
        s = _sampler(
            [{"VmData": 5 * _MIB}, {"VmData": 9 * _MIB}, {"VmData": 7 * _MIB}], clock=clock
        )
        for t in (10.0, 10.05, 10.10):
            clock.now = t
            assert s.step() is True
        snap = s.snapshot()
        assert snap.samples == 3
        assert snap.last_bytes == 7 * _MIB
        assert snap.max_bytes == 9 * _MIB
        assert snap.first_at == 10.0
        assert snap.last_at == 10.10

    def test_field_per_kind_and_vmpeak_on_every_as_sample(self):
        data = _sampler([{"VmData": 1}], kind="data")
        data.step()
        assert data._read.calls == [(4242, ("VmData",))]  # type: ignore[attr-defined]

        as_ = _sampler([{"VmSize": 3 * _MIB, "VmPeak": 8 * _MIB}] * 2, kind="as")
        as_.step()
        as_.step()
        assert as_._read.calls == [(4242, ("VmSize", "VmPeak"))] * 2  # type: ignore[attr-defined]
        snap = as_.snapshot()
        assert snap.last_bytes == 3 * _MIB
        assert snap.peak_bytes == 8 * _MIB

    @pytest.mark.parametrize(
        ("exc", "reason"),
        [
            (FileNotFoundError(), "process_exited"),
            (ProcessLookupError(), "process_exited"),
            (mw.FieldMissing("VmData"), "field_missing"),
            (PermissionError(), "read_error"),
            (OSError(5, "io"), "read_error"),
        ],
    )
    def test_each_read_failure_ends_quietly_with_its_stop_reason(self, exc, reason):
        s = _sampler([{"VmData": _MIB}, exc])
        assert s.step() is True
        assert s.step() is False
        snap = s.snapshot()
        assert snap.stop_reason == reason
        assert snap.samples == 1
        assert snap.last_bytes == _MIB

    def test_reader_returning_a_zombie_shape_is_field_missing(self):
        # A zombie keeps /proc/<pid>/status but drops every Vm* line.
        text = "Name:\tpython\nState:\tZ (zombie)\nPid:\t4242\n"
        with pytest.raises(mw.FieldMissing):
            mw.parse_vm_fields(text, ("VmData",))

    def test_malformed_value_is_field_missing(self):
        with pytest.raises(mw.FieldMissing):
            mw.parse_vm_fields("VmData:\tabc kB\n", ("VmData",))
        with pytest.raises(mw.FieldMissing):
            mw.parse_vm_fields("VmData:\n", ("VmData",))

    def test_parse_vm_fields_reads_kb_as_bytes(self):
        text = "VmPeak:\t  2048 kB\nVmSize:\t1024 kB\nVmData:\t512 kB\n"
        assert mw.parse_vm_fields(text, ("VmSize", "VmPeak")) == {
            "VmSize": 1024 * 1024,
            "VmPeak": 2048 * 1024,
        }

    def test_unexpected_reader_exception_never_raises_out_of_step(self):
        s = _sampler([RuntimeError("boom")])
        assert s.step() is False
        assert s.snapshot().stop_reason == "read_error"

    def test_observer_sees_each_live_sample_and_its_failure_is_swallowed(self):
        seen: list[int] = []

        def obs(value: int) -> None:
            seen.append(value)
            raise RuntimeError("observer bug")

        s = _sampler([{"VmData": 4}, {"VmData": 6}], observer=obs)
        assert s.step() is True
        assert s.step() is True
        assert seen == [4, 6]
        assert s.snapshot().samples == 2


class TestSamplerThread:
    def test_stop_reason_stopped_when_the_event_ends_it(self):
        # Reader never runs dry, so only stop() can end the loop.
        s = mw.MemorySampler(
            1,
            _MIB,
            "data",
            read=lambda pid, names: {"VmData": _MIB},
            interval_s=0.001,
        )
        assert s.start() is True
        snap = s.stop()
        assert snap.stop_reason == "stopped"
        assert snap.samples >= 0
        assert not any(t.name == mw.THREAD_NAME and t.is_alive() for t in threading.enumerate())

    def test_sampling_is_paced_by_the_interval_not_a_busy_loop(self):
        s = mw.MemorySampler(
            1, _MIB, "data", read=lambda pid, names: {"VmData": 1}, interval_s=0.05
        )
        s.start()
        time.sleep(0.3)
        count = s.stop().samples
        assert 2 <= count <= 10

    def test_thread_ends_on_process_exit_and_stop_keeps_that_reason(self):
        s = mw.MemorySampler(1, _MIB, "data", read=_ScriptedReader([{"VmData": 1}]), interval_s=0)
        s.start()
        s._thread.join(2)  # type: ignore[attr-defined]
        assert not s._thread.is_alive()  # type: ignore[attr-defined]
        assert s.stop().stop_reason == "process_exited"

    def test_stalled_read_cannot_change_the_published_snapshot(self):
        release = threading.Event()
        entered = threading.Event()

        def stalled(pid: int, names: tuple[str, ...]) -> dict[str, int]:
            entered.set()
            release.wait(5)
            return {"VmData": 999 * _MIB}

        s = mw.MemorySampler(1, _MIB, "data", read=stalled, interval_s=0, join_timeout_s=0.05)
        s.start()
        assert entered.wait(2)
        snap = s.stop()
        assert snap.samples == 0
        release.set()
        s._thread.join(2)  # type: ignore[attr-defined]
        assert s.snapshot() == snap
        assert s.snapshot().last_bytes is None

    def test_reader_runs_without_the_publication_lock_held(self):
        held: list[bool] = []

        def probe(pid: int, names: tuple[str, ...]) -> dict[str, int]:
            held.append(s._lock.locked())  # type: ignore[attr-defined]
            return {"VmData": 1}

        s = mw.MemorySampler(1, _MIB, "data", read=probe)
        s.step()
        assert held == [False]

    def test_thread_start_failure_yields_no_sampler(self, monkeypatch):
        def boom(self):
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(threading.Thread, "start", boom)
        assert mw.start_sampler(1, _MIB, "data") is None


# ------------------------------------------------------------------ flag rule


def _snap(last_mib: float | None, at: float = 100.0, peak_mib: float | None = None):
    if last_mib is None:
        return mw.MemorySnapshot()
    last = int(last_mib * _MIB)
    return mw.MemorySnapshot(
        samples=3,
        first_at=at - 0.1,
        last_at=at,
        last_bytes=last,
        max_bytes=int((peak_mib or last_mib) * _MIB),
        peak_bytes=None,
        stop_reason="process_exited",
    )


def _evidence(snap, *, cap_mib=1024, observed=100.0, abnormal=True, kind="data"):
    return mw.build_evidence(
        snap, cap_bytes=cap_mib * _MIB, kind=kind, observed_at=observed, abnormal=abnormal
    )


class TestMargin:
    @pytest.mark.parametrize(
        ("cap_mib", "expected_mib"),
        [
            (1024, 102.4),  # 10% above the 64 MiB floor
            (512, 64),  # floor
            (200, 64),
            (128, 64),  # exactly half the cap
            (100, 50),  # bounded to half of a small cap
            (8, 4),
        ],
    )
    def test_margin_is_bounded_and_positive(self, cap_mib, expected_mib):
        assert mw.margin_bytes(cap_mib * _MIB) == pytest.approx(expected_mib * _MIB, abs=1)
        assert mw.margin_bytes(cap_mib * _MIB) > 0


class TestFlagRule:
    def test_last_sample_within_margin_and_fresh_is_true(self):
        ev = _evidence(_snap(1000.0), observed=100.1)
        assert ev.suspected_memory_pressure is True
        assert ev.basis == mw.BASIS_NEAR_CAP

    def test_high_peak_with_low_last_sample_is_false(self):
        ev = _evidence(_snap(100.0, peak_mib=1020.0))
        assert ev.suspected_memory_pressure is False
        assert ev.peak_mb == pytest.approx(1020.0, abs=0.1)

    @pytest.mark.parametrize(
        ("last_mib", "expected"),
        [(921.7, True), (921.5, False), (1100.0, True)],
    )
    def test_threshold_boundary(self, last_mib, expected):
        # Exact-byte boundary: build from bytes to avoid MiB float noise.
        cap = 1024 * _MIB
        threshold = cap - mw.margin_bytes(cap)
        assert mw.suspected_pressure(last_bytes=threshold, age_ms=0.0, cap_bytes=cap, abnormal=True)
        assert not mw.suspected_pressure(
            last_bytes=threshold - 1, age_ms=0.0, cap_bytes=cap, abnormal=True
        )
        ev = _evidence(_snap(last_mib))
        assert ev.suspected_memory_pressure is expected

    def test_small_cap_uses_the_bounded_margin(self):
        # cap 100 MiB: margin is 50 MiB, so 55 MiB qualifies and 45 MiB does not.
        assert _evidence(_snap(55.0), cap_mib=100).suspected_memory_pressure is True
        assert _evidence(_snap(45.0), cap_mib=100).suspected_memory_pressure is False

    @pytest.mark.parametrize(
        ("age_ms", "expected"),
        [(249.9, True), (250.0, True), (250.1, False), (0.0, True)],
    )
    def test_freshness_just_below_at_and_above_the_window(self, age_ms, expected):
        ev = _evidence(_snap(1000.0, at=100.0), observed=100.0 + age_ms / 1000.0)
        assert ev.last_sample_age_ms == pytest.approx(age_ms, abs=0.06)
        assert ev.suspected_memory_pressure is expected

    def test_stale_near_cap_sample_then_delayed_crash_is_false(self):
        # near-cap sample, read failure, release, a late crash: the sample is stale.
        clock = _Clock(100.0)
        s = _sampler(
            [{"VmData": 1000 * _MIB}, OSError(5, "io")],
            clock=clock,
        )
        s.step()
        clock.now = 100.05
        assert s.step() is False
        ev = _evidence(s.snapshot(), observed=100.6)
        assert ev.stop_reason == "read_error"
        assert ev.suspected_memory_pressure is False

    def test_absent_samples_never_set_the_flag(self):
        ev = _evidence(_snap(None))
        assert ev.suspected_memory_pressure is False
        assert ev.samples == 0
        assert ev.basis == mw.BASIS_NO_SAMPLE

    def test_a_fresh_near_cap_timeout_is_false(self):
        ev = _evidence(_snap(1000.0), observed=100.0, abnormal=False)
        assert ev.suspected_memory_pressure is False
        assert ev.last_mb == pytest.approx(1000.0, abs=0.1)

    def test_clock_skew_negative_age_clamps_to_zero(self):
        ev = _evidence(_snap(1000.0, at=100.2), observed=100.0)
        assert ev.last_sample_age_ms == 0.0
        assert ev.suspected_memory_pressure is True

    def test_window_ms_spans_first_to_last_sample(self):
        ev = _evidence(_snap(10.0, at=100.0))
        assert ev.window_ms == pytest.approx(100.0, abs=0.1)

    def test_error_suffix_has_sizes_only_and_none_without_samples(self):
        ev = _evidence(_snap(300.0, peak_mib=400.0), cap_mib=512)
        assert mw.error_suffix(ev) == "; memory: last 300.0 MiB, peak 400.0 MiB of 512 MiB (data)"
        assert mw.error_suffix(_evidence(_snap(None))) == ""


# ---------------------------------------------------------------- wire format


def _result(evidence) -> IsolatedRunResult:
    return IsolatedRunResult(
        outcome="crashed",
        peak_rss_mb=None,
        outputs=None,
        quality_metrics={},
        table_kinds={},
        returncode=-11,
        signal_number=11,
        error="x",
        isolated=True,
        memory_evidence=evidence,
    )


class TestWireFormat:
    def test_memory_evidence_is_the_last_defaulted_field(self):
        import dataclasses

        fields = dataclasses.fields(IsolatedRunResult)
        assert fields[-1].name == "memory_evidence"
        assert fields[-1].default is None
        assert _result(None).memory_evidence is None

    def test_to_dict_round_trips_through_json_with_evidence(self):
        ev = _evidence(_snap(1000.0, peak_mib=1010.0), observed=100.05)
        d = json.loads(json.dumps(ev.to_dict()))
        assert d == ev.to_dict()
        assert set(d) == {
            "cap_mb",
            "kind",
            "last_mb",
            "peak_mb",
            "samples",
            "window_ms",
            "last_sample_age_ms",
            "stop_reason",
            "suspected_memory_pressure",
            "basis",
        }
        assert all(isinstance(v, (int, float, bool, str, type(None))) for v in d.values())
        assert d["suspected_memory_pressure"] is True

    def test_zero_sample_evidence_serializes_nulls(self):
        d = json.loads(json.dumps(_evidence(_snap(None)).to_dict()))
        assert d["samples"] == 0
        assert d["last_mb"] is None
        assert d["peak_mb"] is None
        assert d["last_sample_age_ms"] is None
        assert d["window_ms"] is None
        assert d["suspected_memory_pressure"] is False
        assert d["basis"] == "no sample"

    def test_absent_evidence_is_none_on_the_result(self):
        assert _result(None).memory_evidence is None


# ------------------------------------------------- classifier pinned (test 5)


class TestClassifierUnchanged:
    @pytest.mark.parametrize(
        ("returncode", "stderr", "expected"),
        [
            (-signal.SIGSEGV, "", "crashed"),
            (-signal.SIGSEGV, "MemoryError", "oom_killed"),
            (-signal.SIGKILL, "", "oom_killed"),
            (-signal.SIGABRT, "", "oom_killed"),
            (-signal.SIGTERM, "", "crashed"),
            (-signal.SIGBUS, "", "crashed"),
            (1, "", "crashed"),
            (1, "ValueError: nope", "crashed"),
            (1, "std::bad_alloc", "oom_killed"),
            (1, "arrow::Status OutOfMemory: x", "oom_killed"),
            (1, "cannot allocate memory for thread-local data", "oom_killed"),
            (1, "Unknown error: Wrapping abc failed", "oom_killed"),
            (0, "", "crashed"),
            (137, "Cannot allocate memory", "oom_killed"),
        ],
    )
    def test_table(self, returncode, stderr, expected):
        assert classify_abnormal_exit(returncode, stderr) == expected

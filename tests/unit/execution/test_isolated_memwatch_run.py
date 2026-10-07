"""Sampler lifecycle inside `run_pipeline_isolated`, plus the real-child integration test.

Plan: docs/plans/2026-10-07-oom-vmdata-monitor.md (tests 2 and 4). Outcomes must stay
exactly as on main; only `memory_evidence` and a trailing error suffix are new.
"""

from __future__ import annotations

import os
import re
import select
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest

from decoy_engine.execution import IsolatedRunResult, run_pipeline_isolated
from decoy_engine.execution import _isolated_memwatch as mw

_MIB = 1024 * 1024
_PROC = Path("/proc/self/status")

pytestmark = pytest.mark.skipif(not _PROC.exists(), reason="needs procfs")

_HANGING = "import time\ntime.sleep(999)\n"
_SILENT = "# exits at once with no envelope\n"


def _install(tmp_path, monkeypatch, name: str, source: str) -> None:
    fake_dir = tmp_path / "_fake_workers"
    fake_dir.mkdir(exist_ok=True)
    (fake_dir / f"{name}.py").write_text(source, encoding="utf-8")
    existing = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", str(fake_dir) + (os.pathsep + existing if existing else ""))
    monkeypatch.setattr("decoy_engine.execution._isolated_run._WORKER_MODULE", name)


def _sampler_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == mw.THREAD_NAME and t.is_alive()]


def _run(**kw) -> IsolatedRunResult:
    return run_pipeline_isolated({}, None, engine_version="memwatch-test", **kw)


_MAIN_SILENT_ERROR = "child terminated abnormally (returncode=0); stderr tail: ''"
_SUFFIX = re.compile(r"; memory: last [\d.]+ MiB, peak [\d.]+ MiB of 64 MiB \(data\)$")


class TestLifecycle:
    def test_sampler_is_alive_before_on_spawn_runs(self, tmp_path, monkeypatch):
        _install(tmp_path, monkeypatch, "hang_worker", _HANGING)
        seen: list[int] = []

        def on_spawn(pid: int) -> None:
            seen.append(len(_sampler_threads()))

        _run(mem_cap_bytes=64 * _MIB, on_spawn=on_spawn, timeout_s=0.5)
        assert seen == [1]
        assert _sampler_threads() == []

    def test_uncapped_run_starts_no_sampler_and_has_no_evidence(self, tmp_path, monkeypatch):
        _install(tmp_path, monkeypatch, "silent_worker", _SILENT)
        seen: list[int] = []
        result = _run(on_spawn=lambda pid: seen.append(len(_sampler_threads())))
        assert seen == [0]
        assert result.memory_evidence is None
        assert result.error == _MAIN_SILENT_ERROR
        assert result.outcome == "crashed"
        assert result.returncode == 0

    def test_capped_abnormal_exit_keeps_main_error_text_plus_an_optional_suffix(
        self, tmp_path, monkeypatch
    ):
        _install(tmp_path, monkeypatch, "silent_worker", _SILENT)
        result = _run(mem_cap_bytes=64 * _MIB)
        assert result.outcome == "crashed"
        assert result.returncode == 0
        assert result.error is not None
        assert result.error.startswith(_MAIN_SILENT_ERROR)
        rest = result.error[len(_MAIN_SILENT_ERROR) :]
        assert rest == "" or _SUFFIX.fullmatch(rest)
        assert result.memory_evidence is not None
        assert result.memory_evidence.suspected_memory_pressure in (True, False)
        assert _sampler_threads() == []

    def test_slow_on_spawn_still_cleans_up(self, tmp_path, monkeypatch):
        _install(tmp_path, monkeypatch, "hang_worker", _HANGING)

        def slow(pid: int) -> None:
            time.sleep(0.3)

        result = _run(mem_cap_bytes=64 * _MIB, on_spawn=slow, timeout_s=0.5)
        assert result.outcome == "crashed"
        assert _sampler_threads() == []

    def test_raising_on_spawn_reraises_the_same_object_and_stops_the_sampler(
        self, tmp_path, monkeypatch
    ):
        _install(tmp_path, monkeypatch, "hang_worker", _HANGING)
        boom = RuntimeError("governor callback exploded")
        pids: list[int] = []

        def raiser(pid: int) -> None:
            pids.append(pid)
            raise boom

        with pytest.raises(RuntimeError) as info:
            _run(mem_cap_bytes=64 * _MIB, on_spawn=raiser)
        assert info.value is boom
        assert not Path(f"/proc/{pids[0]}").exists()
        assert _sampler_threads() == []

    def test_timeout_keeps_main_text_and_outcome_and_never_flags(self, tmp_path, monkeypatch):
        _install(tmp_path, monkeypatch, "hang_worker", _HANGING)
        # An 8 MiB cap is far below a live interpreter's VmData, so the last sample is
        # "near the cap" and only the timeout rule keeps the flag off.
        result = _run(mem_cap_bytes=8 * _MIB, timeout_s=0.6)
        assert result.outcome == "crashed"
        assert result.error == "child exceeded timeout_s=0.6s and was killed"
        assert result.returncode == -signal.SIGKILL
        ev = result.memory_evidence
        assert ev is not None
        assert ev.samples > 0
        assert ev.suspected_memory_pressure is False
        assert _sampler_threads() == []

    def test_governor_style_kill_routes_as_on_main(self, tmp_path, monkeypatch):
        _install(tmp_path, monkeypatch, "hang_worker", _HANGING)

        def kill(pid: int) -> None:
            os.kill(pid, signal.SIGKILL)

        result = _run(mem_cap_bytes=64 * _MIB, on_spawn=kill)
        assert result.outcome == "oom_killed"
        assert result.returncode == -signal.SIGKILL
        assert result.signal_number == signal.SIGKILL
        assert result.error is not None
        assert result.error.startswith(
            "child terminated abnormally (returncode=-9, signal=SIGKILL); stderr tail: ''"
        )

    def test_envelope_result_carries_evidence_with_the_flag_off(self, tmp_path, monkeypatch):
        envelope_worker = (
            "import json, sys\n"
            "from pathlib import Path\n"
            "p = Path(sys.argv[1]).parent / 'result.json'\n"
            "p.write_text(json.dumps({'outcome': 'oom_killed', 'peak_rss_mb': 3.0,"
            " 'error': 'MemoryError: x'}))\n"
        )
        _install(tmp_path, monkeypatch, "envelope_worker", envelope_worker)
        # Cap below any live interpreter: a near-cap sample must still not flag a self-report.
        result = _run(mem_cap_bytes=8 * _MIB)
        assert result.outcome == "oom_killed"
        assert result.error == "MemoryError: x"
        assert result.memory_evidence is not None
        assert result.memory_evidence.suspected_memory_pressure is False

    def test_thread_start_failure_leaves_the_run_unaffected(self, tmp_path, monkeypatch):
        _install(tmp_path, monkeypatch, "silent_worker", _SILENT)
        monkeypatch.setattr(
            "decoy_engine.execution._isolated_run.start_sampler", lambda *a, **k: None
        )
        result = _run(mem_cap_bytes=64 * _MIB)
        assert result.memory_evidence is None
        assert result.error == _MAIN_SILENT_ERROR


# --------------------------------------------------------------- integration child

# MAP_PRIVATE matters: Python's default anonymous mmap is shared and VmData ignores it.
# The child applies the cap itself, measures its own baseline, allocates and touches up
# to a measured TOTAL target, reports READY on a dedicated pipe, keeps the allocation until
# ACK, then segfaults with core dumps disabled.
_CHILD = r"""
import ctypes, json, mmap, os, resource, sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text())
kind = payload["rlimit_kind"]
cap = payload["mem_cap_bytes"]
field = "VmData" if kind == "data" else "VmSize"
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
resource.setrlimit(
    resource.RLIMIT_DATA if kind == "data" else resource.RLIMIT_AS, (cap, cap)
)

def total():
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith(field + ":"):
            return int(line.split()[1]) * 1024
    raise SystemExit("no field")

ready_fd = int(os.environ["MW_READY_FD"])
ack_fd = int(os.environ["MW_ACK_FD"])
target = int(os.environ["MW_TARGET_TOTAL"])
need = target - total()
buf = mmap.mmap(-1, need, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
for off in range(0, need, 4096):
    buf[off] = 1
os.write(ready_fd, ("READY %d\n" % total()).encode())
os.read(ack_fd, 3)
ctypes.string_at(0)
"""


class _Handshake:
    """Owns both parent pipe ends; the child ends are closed right after spawn."""

    def __init__(self, target_bytes: int, *, ready_deadline_s=30.0, sample_deadline_s=30.0):
        self.target = target_bytes
        self.ready_deadline_s = ready_deadline_s
        self.sample_deadline_s = sample_deadline_s
        self.ready_r, self.ready_w = os.pipe()
        self.ack_r, self.ack_w = os.pipe()
        self.samples: list[int] = []
        self._cond = threading.Condition()
        self.error: str | None = None
        self.ready_line: str | None = None
        self._thread: threading.Thread | None = None

    @property
    def child_fds(self) -> tuple[int, int]:
        return (self.ready_w, self.ack_r)

    def observe(self, value: int) -> None:
        with self._cond:
            self.samples.append(value)
            self._cond.notify_all()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._coordinate, daemon=True)
        self._thread.start()

    def _coordinate(self) -> None:
        try:
            ready, _, _ = select.select([self.ready_r], [], [], self.ready_deadline_s)
            if not ready:
                self.error = "child never sent READY"
                return
            self.ready_line = os.read(self.ready_r, 64).decode().strip()
            deadline = time.monotonic() + self.sample_deadline_s
            with self._cond:
                while not any(v >= self.target for v in self.samples):
                    left = deadline - time.monotonic()
                    if left <= 0:
                        self.error = "no target-qualified live sample"
                        return
                    self._cond.wait(left)
            os.write(self.ack_w, b"ACK")
        except OSError as exc:
            self.error = f"channel error: {exc!r}"

    def close(self) -> None:
        if self._thread is not None:
            self._thread.join(timeout=self.ready_deadline_s + self.sample_deadline_s + 5)
        for fd in (self.ready_r, self.ready_w, self.ack_r, self.ack_w):
            try:
                os.close(fd)
            except OSError:
                pass


def _fd_open(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


def _with_channel(monkeypatch, hs: _Handshake) -> None:
    real_popen = subprocess.Popen

    def popen(*args, **kwargs):
        proc = real_popen(*args, pass_fds=hs.child_fds, **kwargs)
        # The parent keeps only its own ends, so a dead child yields EOF, not a hang.
        for fd in hs.child_fds:
            os.close(fd)
        return proc

    monkeypatch.setattr("decoy_engine.execution._isolated_run.subprocess.Popen", popen)
    monkeypatch.setattr(mw, "live_observer", hs.observe)
    monkeypatch.setenv("MW_READY_FD", str(hs.ready_w))
    monkeypatch.setenv("MW_ACK_FD", str(hs.ack_r))
    monkeypatch.setenv("MW_TARGET_TOTAL", str(hs.target))


_CAP = 1024 * _MIB


@pytest.mark.parametrize("kind", ["data", "as"])
class TestRealChild:
    def _drive(self, tmp_path, monkeypatch, kind, target):
        _install(tmp_path, monkeypatch, "memchild_worker", _CHILD)
        hs = _Handshake(target)
        _with_channel(monkeypatch, hs)
        hs.start()
        try:
            result = _run(mem_cap_bytes=_CAP, rlimit_kind=kind, timeout_s=90)
        finally:
            hs.close()
        assert hs.error is None, hs.error
        assert hs.ready_line is not None and hs.ready_line.startswith("READY ")
        return result

    def test_crash_near_the_cap_records_a_qualifying_sample(self, tmp_path, monkeypatch, kind):
        margin = mw.margin_bytes(_CAP)
        result = self._drive(tmp_path, monkeypatch, kind, _CAP - margin // 2)
        assert result.outcome == "crashed"
        assert result.signal_number == signal.SIGSEGV
        ev = result.memory_evidence
        assert ev is not None
        assert ev.samples > 0
        assert ev.last_mb is not None
        assert ev.last_mb * _MIB >= _CAP - margin, "no qualifying near-cap sample was recorded"
        assert ev.last_sample_age_ms is not None
        expected = mw.suspected_pressure(
            last_bytes=int(ev.last_mb * _MIB),
            age_ms=ev.last_sample_age_ms,
            cap_bytes=_CAP,
            abnormal=True,
        )
        assert ev.suspected_memory_pressure is expected
        assert result.error is not None and "memory: last" in result.error

    def test_crash_far_from_the_cap_is_not_flagged(self, tmp_path, monkeypatch, kind):
        result = self._drive(tmp_path, monkeypatch, kind, _CAP // 4)
        assert result.outcome == "crashed"
        assert result.signal_number == signal.SIGSEGV
        ev = result.memory_evidence
        assert ev is not None and ev.samples > 0
        assert ev.suspected_memory_pressure is False


class TestHandshakeCleanup:
    def test_failed_handshake_closes_every_end(self):
        hs = _Handshake(_CAP, ready_deadline_s=0.2)
        fds = (hs.ready_r, hs.ready_w, hs.ack_r, hs.ack_w)
        hs.start()
        hs.close()
        assert hs.error == "child never sent READY"
        assert not any(_fd_open(fd) for fd in fds)

    def test_a_child_that_never_reports_is_killed_and_the_channel_released(
        self, tmp_path, monkeypatch
    ):
        _install(tmp_path, monkeypatch, "hang_worker", _HANGING)
        hs = _Handshake(_CAP, ready_deadline_s=0.3)
        fds = (hs.ready_r, hs.ready_w, hs.ack_r, hs.ack_w)
        _with_channel(monkeypatch, hs)
        hs.start()
        try:
            result = _run(mem_cap_bytes=_CAP, timeout_s=0.8)
        finally:
            hs.close()
        assert hs.error == "child never sent READY"
        assert result.error == "child exceeded timeout_s=0.8s and was killed"
        assert result.memory_evidence is not None
        assert result.memory_evidence.suspected_memory_pressure is False
        assert not any(_fd_open(fd) for fd in fds)
        assert _sampler_threads() == []

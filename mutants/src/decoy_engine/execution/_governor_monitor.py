"""Sprint B4 runtime governor: the supervisor-side `/proc` poller.

Split out of `_governor.py` to hold the 600-LOC orchestration cap
(CLAUDE.md "Engineering best practices"; same split rationale
`_pipeline_routing.py`'s module docstring gives for its own
`_pipeline_routing_signals.py` / `_pipeline_chunk_route.py` siblings).
`_governor.py` owns the reroute LADDER (which route to try next, when to
give up); this module owns the ONE mechanical primitive the ladder relies
on to detect and act on a hard-threshold cross: a background thread in the
DRIVER process polling a CHILD pid's `/proc/<pid>/status` and `SIGKILL`ing
it from outside its address space (see `_governor.py`'s module docstring
for why this must be an external supervisor, not an in-process watchdog).

MED-1 (dennis review): the kill path confirms `PPid == os.getpid()` from
the SAME `/proc/<pid>/status` snapshot that crossed the threshold before
issuing `os.kill` -- between the real child being reaped and this
monitor's `stop()` being called, the kernel can recycle the pid to an
unrelated same-uid process, and killing that would be a same-uid
arbitrary kill, not an OOM defense.
"""

from __future__ import annotations

import logging
import os
import signal
import threading

__all__ = ["_RssMonitor", "_read_child_status", "_read_child_vmrss_bytes"]

_logger = logging.getLogger(__name__)


def _read_child_status(pid: int) -> tuple[int, int | None] | None:
    """Read this CHILD pid's `/proc/<pid>/status` ONCE, returning
    `(vmrss_bytes, ppid)`.

    MED-1: parsing both fields from the SAME snapshot means the PPid
    identity check `_RssMonitor._maybe_kill_child` makes right before
    killing is never racing a second `/proc` read. `ppid` is `None` only
    if the status has no `PPid:` line (not expected on Linux, but not
    fatal) -- the kill guard treats that like an explicit mismatch: no
    kill.

    Must be robust to the process exiting between us learning its pid and
    this read (normal completion racing the poll, or a kill already in
    flight) -- `/proc/<pid>/status` disappearing mid-read is the expected
    end condition for this poller, never an error to raise. Returns
    `None` (never a partial tuple) once the pid is gone, or the status
    has no `VmRSS:` line (kernel/zombie edge case).
    """
    vmrss_bytes: int | None = None
    ppid: int | None = None
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    # proc(5): "VmRSS" is reported in kB regardless of the
                    # host page size.
                    vmrss_bytes = int(line.split()[1]) * 1024
                elif line.startswith("PPid:"):
                    ppid = int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    if vmrss_bytes is None:
        return None  # no VmRSS line at all (kernel/zombie edge case) -- treat as gone
    return vmrss_bytes, ppid


def _read_child_vmrss_bytes(pid: int) -> int | None:
    """VmRSS-only view of `_read_child_status` -- a useful primitive on its
    own, and pins the "VmRSS, never cgroup memory.current" contract (module
    docstring) under one stable, directly-testable name."""
    status = _read_child_status(pid)
    return status[0] if status is not None else None


class _RssMonitor:
    """Supervisor-side poller for ONE child pid. Runs in a background thread
    in the DRIVER process (not the child), so it can `SIGKILL` the child from
    outside its address space regardless of what the child's own interpreter
    is doing at the moment the threshold is crossed (see module docstring).

    Never raises out of its polling loop: a child that exits mid-poll (normal
    completion, or a kill already delivered by someone else) just ends the
    loop, matching `run_pipeline_isolated`'s own contract that a vanished
    child is a normal, cleanly-classified outcome, not a driver exception.
    """

    def __init__(self, pid: int, hard_threshold_bytes: int, poll_interval_s: float) -> None:
        self._pid = pid
        self._hard_threshold_bytes = hard_threshold_bytes
        self._poll_interval_s = poll_interval_s
        self._stop_event = threading.Event()
        self._tripped_event = threading.Event()
        self._peak_bytes = 0
        self._started = False  # LOW-3: did Thread.start() actually succeed?
        self._thread = threading.Thread(
            target=self._run, name=f"decoy-governor-rss-{pid}", daemon=True
        )

    def start(self) -> None:
        self._thread.start()
        # Only reached if `start()` didn't raise (e.g. thread exhaustion) --
        # `stop()` uses this to decide whether `join()` is even legal to call.
        self._started = True

    def stop(self) -> None:
        """Idempotent, bounded join -- called unconditionally once the run
        this monitor was watching has returned, whether or not it tripped.

        LOW-3: if `start()` itself raised (thread exhaustion), `self._thread`
        was never started, and `Thread.join()` on a never-started thread
        raises `RuntimeError` -- masking the ORIGINAL `start()` failure
        propagating out of `_run_one_rung`'s `finally`. `self._started` is
        only set once `start()` succeeds, so this is a clean no-op then:
        nothing was ever polling, so there is nothing to stop.
        """
        self._stop_event.set()
        if not self._started:
            return
        self._thread.join(timeout=self._poll_interval_s + 5.0)

    @property
    def tripped(self) -> bool:
        return self._tripped_event.is_set()

    @property
    def peak_observed_bytes(self) -> int:
        """The highest VmRSS this monitor actually sampled. May under-report
        the child's true peak between polls (point-in-time sampling, same
        limitation `out_of_core._budget.check_temp_disk_budget` documents for
        its own poll-at-boundaries disk check) -- the hard-threshold kill is
        the safety property; this number is diagnostic/telemetry only."""
        return self._peak_bytes

    def _run(self) -> None:
        while not self._stop_event.is_set():
            status = _read_child_status(self._pid)
            if status is None:
                return  # child is gone -- stop cleanly, nothing left to poll
            rss, ppid = status
            if rss > self._peak_bytes:
                self._peak_bytes = rss
            if rss >= self._hard_threshold_bytes:
                self._maybe_kill_child(ppid)
                return
            self._stop_event.wait(self._poll_interval_s)

    def _maybe_kill_child(self, ppid: int | None) -> None:
        """MED-1: confirm this pid is STILL actually our child before
        killing it. Between the child being reaped (`proc.communicate()`
        returning in `run_pipeline_isolated`) and this monitor's `stop()`
        being called, the kernel can recycle the pid to an unrelated
        same-uid process (another decoy child, or the platform worker) --
        `os.kill` on THAT process would be a same-uid arbitrary kill, not
        an OOM defense. `ppid` comes from the SAME `/proc/<pid>/status`
        snapshot that crossed the threshold, so this introduces no extra
        TOCTOU window: a recycled pid's real parent is whatever spawned
        IT, never this driver, so `ppid != os.getpid()` reliably means
        "not our child anymore." `ppid is None` (no `PPid:` line -- not
        expected on Linux) is treated the same: unconfirmed, so no kill.
        """
        if ppid != os.getpid():
            _logger.warning(
                "governor: pid=%d VmRSS crossed the hard threshold but its "
                "PPid=%r does not match this driver's own pid=%d -- refusing "
                "to kill a pid that is no longer (or never was) our child.",
                self._pid,
                ppid,
                os.getpid(),
            )
            return
        # The threshold-cross decision is made from the read above,
        # independent of whether the kill itself lands: a race where the
        # child exits between that read and this call still means we
        # correctly detected a hard-threshold cross and tried to act on it.
        self._tripped_event.set()
        try:
            os.kill(self._pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # already gone -- nothing left to kill, not an error

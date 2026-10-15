"""Run one worker process under an external supervisor that always yields a record.

Plan: docs/plans/2026-10-15-d4-scale-proof.md section 3.1. The measurement that
decides "did it fit" must come from OUTSIDE the process being measured, and a worker
that is OOM-killed, signalled or hung must still produce a record, so this module
never imports the engine or pyarrow (a heavy supervisor would also inflate the
inherited `ru_maxrss`, which survives exec on Linux).

Peak RSS = the kernel's `ru_maxrss` from `wait4` for the worker (covers its whole
lifetime including a kill), cross-checked against the highest `VmHWM` the poll loop
saw. `rc == 0` is NOT success here: the caller decides success from the result file
the worker wrote.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_POLL_S = 0.25


@dataclass
class WorkerOutcome:
    returncode: int | None
    term_signal: int | None
    timed_out: bool
    rss_killed: bool
    wall_s: float
    peak_rss_mb: float
    peak_rss_poll_mb: float
    result: dict[str, Any] | None
    result_error: str | None


def _read_status_kb(pid: int, field: str) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith(field + ":"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _load_result(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """A missing, unreadable or non-object result is reported, never guessed at."""
    if not path.exists():
        return None, "missing"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None, "malformed"
    if not isinstance(data, dict):
        return None, "malformed"
    return data, None


def run_worker(
    cmd: list[str],
    *,
    env: dict[str, str],
    result_path: Path,
    timeout_s: float | None,
    rss_kill_mb: float | None,
) -> WorkerOutcome:
    result_path.unlink(missing_ok=True)
    start = time.monotonic()
    # Own session so a kill takes the worker's whole process group (DuckDB/rayon threads
    # are in-process, but a future helper subprocess would not be left running).
    proc = subprocess.Popen(cmd, env=env, start_new_session=True)  # noqa: S603
    timed_out = rss_killed = False
    poll_peak_kb = 0
    status = 0
    rusage = None
    while True:
        pid, status, rusage = os.wait4(proc.pid, os.WNOHANG)
        if pid:
            break
        poll_peak_kb = max(poll_peak_kb, _read_status_kb(proc.pid, "VmHWM"))
        elapsed = time.monotonic() - start
        if not (timed_out or rss_killed):
            if timeout_s is not None and elapsed > timeout_s:
                timed_out = True
            elif rss_kill_mb is not None and poll_peak_kb / 1024.0 > rss_kill_mb:
                rss_killed = True
            if timed_out or rss_killed:
                _kill_group(proc.pid)
        time.sleep(_POLL_S)
    wall_s = time.monotonic() - start
    proc.returncode = 0  # reaped by wait4 above; stop Popen from warning about it
    term_signal = os.WTERMSIG(status) if os.WIFSIGNALED(status) else None
    returncode = os.WEXITSTATUS(status) if os.WIFEXITED(status) else None
    result, err = _load_result(result_path)
    return WorkerOutcome(
        returncode=returncode,
        term_signal=term_signal,
        timed_out=timed_out,
        rss_killed=rss_killed,
        wall_s=wall_s,
        peak_rss_mb=round(rusage.ru_maxrss / 1024.0, 1) if rusage else 0.0,
        peak_rss_poll_mb=round(poll_peak_kb / 1024.0, 1),
        result=result,
        result_error=err,
    )


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def classify_failure(o: WorkerOutcome) -> dict[str, Any] | None:
    """Why this run is not a success, or None when the worker reported a clean finish.

    Order matters: a supervisor-imposed kill is named as such before the generic
    signal, and an rc=0 with no usable result is still a failure.
    """
    if o.timed_out:
        return {"kind": "timeout"}
    if o.rss_killed:
        return {"kind": "rss_ceiling_kill"}
    if o.term_signal is not None:
        # SIGKILL with no supervisor kill is the kernel OOM-killer's signature.
        kind = "killed_sigkill_probable_oom" if o.term_signal == signal.SIGKILL else "signal"
        return {"kind": kind, "signal": o.term_signal}
    if o.result is None:
        return {"kind": f"result_{o.result_error}", "returncode": o.returncode}
    if not o.result.get("completed_inner"):
        err = o.result.get("error") or {}
        return {"kind": "worker_reported_failure", "returncode": o.returncode, "error": err}
    if o.returncode != 0:
        return {"kind": "nonzero_exit_after_result", "returncode": o.returncode}
    return None

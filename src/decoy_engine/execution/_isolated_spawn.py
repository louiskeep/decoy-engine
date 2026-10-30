"""Spawn one isolated child and classify how it ended (split out of `_isolated_run`).

The child is a fresh `python -m` process (pattern and citations in `_isolated_run` and
`_isolated_common`). This module owns the process lifecycle only: it starts the child with the
capped environment, hands its PID to `on_spawn`, enforces the wall-clock bound, reaps it on
every path, and decides whether the child self-reported a usable envelope. It never touches
staging or publication: disposal of the staged directory belongs to the caller's
`StagingLease`, and promotion to `_isolated_run`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from decoy_engine.execution._isolated_commit import read_envelope
from decoy_engine.execution._isolated_commit import signal_name as _signal_name
from decoy_engine.execution._isolated_common import (
    CAPPED_ENV,
    ISOLATED_WORKER_ENV,
    RESULT_FILENAME,
    IsolatedRunResult,
    abnormal_exit_text,
    classify_abnormal_exit,
)
from decoy_engine.execution._isolated_memwatch import error_suffix, evidence_of, start_sampler

_WORKER_MODULE = "decoy_engine.execution._isolated_worker"

_SELF_REPORTED = ("completed", "oom_killed", "crashed")


@dataclass(frozen=True)
class SelfReport:
    """The child exited 0 with a usable result envelope; promotion is the caller's job."""

    pid: int
    memory_evidence: Any
    envelope: dict[str, Any]


def spawn_and_classify(
    payload_path: Path,
    *,
    on_spawn: Callable[[int], None] | None,
    timeout_s: float | None,
    mem_cap_bytes: int | None,
    rlimit_kind: str,
) -> SelfReport | IsolatedRunResult:
    # HIGH-1: the envelope is a known FILE in work_root (payload_path's own
    # parent), never stdout's last line -- see `_isolated_common.RESULT_FILENAME`'s
    # docstring for the stdout-contamination class this replaces. stdout/stderr
    # below remain captured for diagnostics only.
    result_path = payload_path.parent / RESULT_FILENAME
    cmd = [sys.executable, "-m", _WORKER_MODULE, str(payload_path)]
    # _CAPPED_ENV must land BEFORE the child interpreter starts: glibc reads
    # MALLOC_ARENA_MAX at startup and pyarrow reads ARROW_DEFAULT_MEMORY_POOL
    # at import, so `env=` at spawn time is the only place this can work.
    # ISOLATED_WORKER_ENV marks the child as "inside an isolated worker" so a
    # re-entrant `run_pipeline_isolated` call (probe/governor routing inside
    # the child's `run_pipeline`) runs in-process instead of spawning a
    # grandchild -- see that marker's docstring for the runaway it prevents.
    env = {**os.environ, **CAPPED_ENV, ISOLATED_WORKER_ENV: "1"}
    proc = subprocess.Popen(  # noqa: S603
        cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )

    sampler = None
    try:
        # Started before on_spawn so a callback that kills the child still leaves samples;
        # inside the try so a construction error still kills and reaps the child.
        if mem_cap_bytes is not None:
            sampler = start_sampler(proc.pid, mem_cap_bytes, rlimit_kind)
        if on_spawn is not None:
            # The child's PID is live NOW, mid-run -- this is the hook a
            # governor (or a test simulating one) uses to SIGKILL it before
            # it can report. MED-3: this call is INSIDE the try, so a
            # raising callback still hits the `except BaseException` below
            # and gets the child killed + reaped rather than leaked.
            on_spawn(proc.pid)
        stdout, stderr = proc.communicate(timeout=timeout_s)
        # Stop before envelope work so late parsing cannot age or extend the evidence.
        finished = sampler.finish() if sampler else None
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        finished = sampler.finish() if sampler else None
        return IsolatedRunResult(
            outcome="crashed",
            peak_rss_mb=None,
            outputs=None,
            quality_metrics={},
            table_kinds={},
            returncode=proc.returncode,
            signal_number=None,
            error=f"child exceeded timeout_s={timeout_s}s and was killed",
            isolated=True,
            pid=proc.pid,
            memory_evidence=evidence_of(finished, abnormal=False),
        )
    except BaseException:
        # MED-3: on_spawn raised (a governor bug, not the child's fault).
        # Guarantee the child is killed and reaped here -- otherwise it is
        # a zombie + leaked stdout/stderr pipe fds the instant this
        # exception propagates past a caller who never gets a PID to
        # clean up themselves.
        proc.kill()
        proc.communicate()
        if sampler:
            sampler.finish()
        raise

    envelope = read_envelope(result_path)
    if proc.returncode == 0 and envelope is not None and envelope.get("outcome") in _SELF_REPORTED:
        return SelfReport(proc.pid, evidence_of(finished, abnormal=False), envelope)

    # The child died too hard to self-report (a harder rlimit trip the kernel turned into a
    # signal, or an external SIGKILL -- the governor simulation teeth). Classify from the
    # outside; nothing was committed, and the caller's lease disposes whatever it staged.
    outcome = classify_abnormal_exit(proc.returncode, stderr)
    signal_number = -proc.returncode if proc.returncode < 0 else None
    signal_name = _signal_name(signal_number)
    # stderr/stdout feed the classifier and the route-marker scan only; their text is
    # never exposed (a crashing strategy can print the data value it choked on).
    evidence = evidence_of(finished, abnormal=True)
    return IsolatedRunResult(
        outcome=outcome,
        peak_rss_mb=None,  # unrecoverable: the child is gone and never reported
        outputs=None,
        quality_metrics={},
        table_kinds={},
        returncode=proc.returncode,
        signal_number=signal_number,
        error=(
            abnormal_exit_text(proc.returncode, signal_name, stderr, stdout)
            + (error_suffix(evidence) if evidence else "")
        ),
        isolated=True,
        pid=proc.pid,
        memory_evidence=evidence,
    )

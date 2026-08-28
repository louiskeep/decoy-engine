"""Shared constants, classification, and the result contract for isolated runs.

Pattern: ported from `scripts/fk_memory_probe.py` (the OOM-avoidance routing
redesign spec, `docs/plans/2026-07-10-oom-avoidance-routing-redesign.md` §12,
names this the reference isolation primitive to promote from benchmark
harness to production execution). Specifically ported: `_peak_rss_mb`'s
VmHWM-over-`ru_maxrss` read (a worker spawned by a large parent inherits the
parent's `ru_maxrss` high-water mark across `execve`, over-reporting by
hundreds of MB; VmHWM belongs to the process's own `mm`, which `execve`
recreates), the `_RLIMITS` / `_apply_mem_cap` hard-cap application, the
`_CAPPED_ENV` allocator pinning (glibc arenas + Arrow's default pool both
reserve address space far beyond live data, so an unpinned capped run OOMs on
*reservation*, not real usage), and the two-tier outcome classification
(a worker that catches its own memory failure self-reports; a worker killed
harder than it can catch -- SIGKILL, SIGABRT, or a memory-shaped stderr
marker -- is classified from the outside by exit signal).

`IsolatedRunResult` is the NEW piece the probe does not need (it has no
caller other than its own CLI report): the contract `run_pipeline_isolated`
returns, and what Sprint 1a-part-2 (platform `queue_worker` wiring) will
consume.
"""

from __future__ import annotations

import errno
import re
import resource
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import duckdb

from decoy_engine.execution._row_errors import RowErrorRecord

if TYPE_CHECKING:
    import pyarrow as pa

__all__ = [
    "ISOLATED_WORKER_ENV",
    "RESULT_FILENAME",
    "RLIMIT_KINDS",
    "IsolatedRunOutcome",
    "IsolatedRunResult",
    "apply_mem_cap",
    "classify_abnormal_exit",
    "is_memory_failure",
    "peak_rss_mb",
]

# Set in the environment of every child `run_pipeline_isolated` spawns. A
# worker's `run_pipeline` can re-enter the routing layer (probe/governor),
# which calls `run_pipeline_isolated` again -- with no "already isolated"
# signal, each level spawns another child, a self-multiplying subprocess
# chain that saturated the host and had to be killed by hand (engine-
# efficiency streaming qualification, 2026-08-28). `run_pipeline_isolated`
# reads this marker and runs in-process instead of spawning when it is
# already inside an isolated worker, which breaks the chain at depth 1 (the
# re-entrant probe then reports a non-isolated measurement and routing falls
# back to the in-process estimator, never a grandchild).
ISOLATED_WORKER_ENV = "DECOY_INSIDE_ISOLATED_WORKER"

# The envelope transport contract (dennis review HIGH-1): a known FILE in
# work_root, never the last line of stdout. Any extra child stdout (atexit
# handlers, BLAS/OpenMP teardown chatter, a chatty provider, a stray user
# print inside a strategy) used to be able to push the real envelope off
# the "last line" a naive parser expected, silently turning a COMPLETED job
# into a `crashed` misclassification whose staged output then got rmtree'd
# (the SC7b stdout-contamination class). stdout/stderr are still captured
# by the driver, but purely for diagnostics now -- never parsed as the
# result transport. Both worker and driver derive this path the same way:
# `payload_path.parent / RESULT_FILENAME` (work_root is the payload's own
# parent directory).
RESULT_FILENAME = "result.json"

IsolatedRunOutcome = Literal["completed", "oom_killed", "crashed"]

# Same two rlimit knobs fk_memory_probe exposes (`_RLIMITS`): 'data' (default)
# caps brk + anonymous mmaps on Linux >= 4.7 -- the allocations that actually
# OOM here -- without tripping on pyarrow/DuckDB's reserved-but-untouched
# address space the way 'as' can; 'as' is the blunter total-address-space cap.
RLIMIT_KINDS: dict[str, int] = {"as": resource.RLIMIT_AS, "data": resource.RLIMIT_DATA}

# Ported verbatim from fk_memory_probe._CAPPED_ENV: MALLOC_ARENA_MAX pins
# glibc to the system allocator's per-thread-arena count and
# ARROW_DEFAULT_MEMORY_POOL="system" retires Arrow's jemalloc/mimalloc pool,
# both of which otherwise reserve address space far beyond live data (~2.5 GB
# measured at ~400 MB RSS in the probe's harness). Must be set BEFORE the
# child process starts (glibc reads MALLOC_ARENA_MAX at startup, pyarrow
# reads ARROW_DEFAULT_MEMORY_POOL at import) -- driver-side via subprocess
# `env=`, never worker-side `os.environ[...] = ...` after the interpreter is
# already running (per Sprint 1a acceptance addition in the spec's §12).
CAPPED_ENV: dict[str, str] = {
    "ARROW_DEFAULT_MEMORY_POOL": "system",
    "MALLOC_ARENA_MAX": "2",
}

# Same three under-cap failure shapes fk_memory_probe documents evidence for
# (`_MEMORY_ERRORS` / `_MEMORY_ERROR_MARKERS` / `_MEMORY_ERROR_PATTERNS` /
# `_GLIBC_TLS_OOM_MARKER`): pyarrow/numpy MemoryError subclasses and DuckDB's
# own OutOfMemoryException are the common case; OpenSSL's EVP_MD_CTX copy
# (hashlib, masking the FK key) and Arrow's arrow-to-pandas value conversion
# both raise their own error type under a hard cap instead of MemoryError,
# and glibc's per-thread TLS setup aborts the process outright. See
# fk_memory_probe.py's inline comments for the full reproduction evidence;
# reproduced here without re-deriving it.
_MEMORY_ERRORS: tuple[type[BaseException], ...] = (MemoryError, duckdb.OutOfMemoryException)
_MEMORY_ERROR_MARKERS: tuple[str, ...] = (
    "not able to copy ctx",
    "digital envelope routines",
)
_MEMORY_ERROR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Unknown error: Wrapping \S+ failed"),
)
_GLIBC_TLS_OOM_MARKER = "cannot allocate memory for thread-local data"

# Stderr markers a driver-side (outside-the-process) classification checks
# when a worker was killed too hard to self-report -- the SIGKILL/SIGABRT
# case fk_memory_probe's `_classify_capability_outcome` handles the same way.
_ABNORMAL_EXIT_MEMORY_MARKERS: tuple[str, ...] = (
    "MemoryError",
    "bad_alloc",
    "OutOfMemory",
    "Cannot allocate",
    "ENOMEM",
    "arrow::Status OutOfMemory",
    _GLIBC_TLS_OOM_MARKER,
    *_MEMORY_ERROR_MARKERS,
)

_MIB = 1024 * 1024


def peak_rss_mb() -> float:
    """This process's true peak RSS in MB. Ported from `fk_memory_probe._peak_rss_mb`.

    VmHWM, not `ru_maxrss`: on Linux the rusage high-water mark survives
    `execve`, so a worker spawned by a large parent inherits the PARENT's
    peak and over-reports by hundreds of MB (measured in the probe: a child
    of a 600 MB parent reported `ru_maxrss` ~610 MB vs VmHWM ~9 MB). VmHWM
    belongs to the process's own `mm`, which `execve` recreates, so it is
    parent-independent -- this is the whole reason `run_pipeline_isolated`
    spawns a fresh `python -m` child rather than reusing a warm worker
    process. `ru_maxrss` stays as the non-Linux fallback.
    """
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError):
        pass
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def apply_mem_cap(cap_bytes: int, rlimit_kind: str) -> None:
    """Hard-cap this process's memory before any workload allocation.

    Must be the first statement in the worker's `main()` after argument
    parsing -- before sources are loaded or `run_pipeline` is called (ruling
    per spec §12: "`resource.setrlimit` applied in-child before workload
    allocation").
    """
    resource.setrlimit(RLIMIT_KINDS[rlimit_kind], (cap_bytes, cap_bytes))


def is_memory_failure(exc: BaseException) -> bool:
    """True iff `exc` is one of the documented under-cap failure shapes.

    Ported from `fk_memory_probe._is_memory_failure` unchanged: the worker
    calls this to self-classify a caught exception as `oom_killed` (exits 0
    with a clean diagnostic) versus `crashed` (a genuine bug, re-raised as
    data in the result envelope but NOT hidden behind an OOM label).
    """
    if isinstance(exc, _MEMORY_ERRORS):
        return True
    if isinstance(exc, OSError) and exc.errno == errno.ENOMEM:
        return True
    message = str(exc)
    if any(marker in message for marker in _MEMORY_ERROR_MARKERS):
        return True
    return any(pattern.search(message) for pattern in _MEMORY_ERROR_PATTERNS)


def classify_abnormal_exit(returncode: int, stderr: str) -> IsolatedRunOutcome:
    """Classify a child that exited WITHOUT producing a result envelope.

    This is the driver-side half of the two-tier classification
    fk_memory_probe's `_classify_capability_outcome` established: a worker
    that catches its own memory failure self-reports via the envelope (never
    reaches this function). This function only runs when the child died too
    hard to write one -- a harder rlimit trip that the kernel or glibc turned
    into a signal, or a governor's external SIGKILL (the mid-run-kill teeth
    test). Only a memory-SHAPED abnormal exit is classified `oom_killed`, so
    a genuine non-memory crash (segfault, unrelated signal) cannot masquerade
    as the expected OOM path:

    - a positive returncode (uncaught Python exception, no envelope) is
      `oom_killed` only when a memory marker is in stderr, else `crashed`;
    - a signal death (`returncode < 0`) is `oom_killed` for SIGKILL (the
      kernel OOM killer, or an external governor's kill -- both mean "the
      process is gone before it could report," which routes the same way)
      and SIGABRT (C++ `bad_alloc` / `std::terminate`), or for any signal
      whose stderr shows a memory marker; any other signal is classified
      `crashed`.
    """
    if any(marker in stderr for marker in _ABNORMAL_EXIT_MEMORY_MARKERS):
        return "oom_killed"
    if any(pattern.search(stderr) for pattern in _MEMORY_ERROR_PATTERNS):
        return "oom_killed"
    if returncode in (-signal.SIGKILL, -signal.SIGABRT):
        return "oom_killed"
    return "crashed"


@dataclass(frozen=True)
class IsolatedRunResult:
    """The contract `run_pipeline_isolated` returns.

    Sprint 1a-part-2 (platform `queue_worker` wiring, behind
    `isolated_execution_enabled`) consumes this directly: `outcome`
    dispatches the reroute ladder (`completed` -> publish; `oom_killed` ->
    reroute to a bounded route; `crashed` -> surface `error` to the job
    record), `peak_rss_mb` feeds the B5 telemetry loop (a per-job, fresh-
    `execve` peak -- exactly what a shared warm worker cannot produce, per
    spec §11's contamination finding), and `isolated=False` tells the
    telemetry loop this sample is NOT trustworthy (the in-process fallback
    path, see `run_pipeline_isolated`'s docstring) so it must not be folded
    into `k_path` recalibration.

    `outputs` / `quality_metrics` / `table_kinds` mirror `ExecutionResult`'s
    fields but are reconstructed from the child's staged Parquet + JSON
    envelope rather than the object itself (an `ExecutionResult` carries
    non-JSON-safe fields -- `StrategyTimingRecord`, `QualityWarning` -- that
    do not need to cross the process boundary for the isolation primitive to
    be useful; `timings`/`warnings` are dropped for this first cut, a known
    part-1 scope limitation, not an oversight).

    `row_errors` (dennis review MED-4) is the exception: it IS carried
    across the boundary, unlike `timings`/`warnings`, because it is the
    user-facing quarantine surface (bucketize/date_shift `format_error`,
    code_set `mask_error`) -- silently dropping it would make a job's
    quarantine reporting depend on whether it happened to run isolated.
    The worker stages it as `row_errors.json` alongside the output Parquet
    (same staging directory, read back before commit-or-discard, same
    discipline as `outputs`); each `RowErrorRecord` field is a plain
    str/int, so unlike `StrategyTimingRecord`/`QualityWarning` it needs no
    richer serialization.

    Caveat for B5 (LOW-1): `outputs` round-trips through a Parquet
    write-then-read rather than being the in-memory `pa.Table` `run_pipeline`
    produced directly, so exotic schema metadata (e.g. pandas index
    metadata, some dictionary-encoding choices) is not guaranteed byte-
    identical to the in-process result -- callers doing strict schema
    comparison against the in-process path should be aware of this.
    """

    outcome: IsolatedRunOutcome
    peak_rss_mb: float | None
    outputs: dict[str, pa.Table] | None
    quality_metrics: dict[str, Any]
    table_kinds: dict[str, str]
    returncode: int | None
    signal_number: int | None
    error: str | None
    isolated: bool
    pid: int | None = None
    # Populated (non-None) only for a completed run with an `output_dir`
    # commit: the final on-disk location the staged outputs were rename-
    # committed to. None on any other outcome -- the whole point of the
    # staging contract is that nothing lands here unless the run completed
    # cleanly (spec §12 ruling 3).
    committed_output_dir: str | None = None
    # MED-4: table-attributed per-row strategy failures, carried across the
    # process boundary (see docstring above). Empty on any non-`completed`
    # outcome -- there is nothing to attribute a row error to.
    row_errors: tuple[RowErrorRecord, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)

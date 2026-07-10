"""`run_pipeline_isolated`: a fresh-process wrapper around `run_pipeline`.

Pattern: ported from `scripts/fk_memory_probe.py` (full citation in
`_isolated_common`'s module docstring). Sprint 1a decision (RATIFIED,
`docs/plans/2026-07-10-oom-avoidance-routing-redesign.md` §12): promote the
probe's fresh-`execve`-per-job isolation from benchmark harness to a
production execution primitive, because it is the only option that delivers
all three things the OOM-avoidance redesign's later sprints need -- a clean
per-job peak (B5 telemetry), an externally SIGKILL-able unit (B4 governor),
and fresh-`execve` HWM semantics that do not read a warm worker's
historical high-water mark (the ru_maxrss-vs-VmHWM contamination the probe
itself documents).

Isolation contract:

  1. `config` and `sources` (which can be large) are NEVER passed via argv --
     written to a temp payload file the worker reads (config as JSON,
     sources as Parquet), matching the probe's own `--source-dir` convention
     of pointing a worker at on-disk input rather than materializing it in
     command-line text.
  2. `_CAPPED_ENV` (MALLOC_ARENA_MAX / ARROW_DEFAULT_MEMORY_POOL) is set via
     `subprocess.Popen(..., env=...)`, i.e. driver-side, before the child
     interpreter starts -- both env vars are read at process/import time, so
     setting them after the child is running would be a no-op.
  3. The child's PID is handed to `on_spawn` (if given) the moment
     `Popen` returns, so a caller (a test simulating the governor, or the
     real governor in a later sprint) can act on it while the run is still
     in flight; this module does not poll or kill on its own -- that is B4's
     job, deliberately out of Sprint 1a-part-1 scope.
  4. Staging + commit (spec §12 ruling 3): the child writes its output
     tables to a staging directory this driver chose, never the caller's
     `output_dir`. Only when the child exits cleanly with a `completed`
     envelope does this driver perform ONE atomic `os.replace` (the same
     single-rename pattern `ParquetTransactionalSink.commit` uses) to
     publish staging as `output_dir`. A SIGKILL at any point before that
     rename leaves nothing at `output_dir` -- the staging directory is
     simply discarded, orphaned or not.
  5. Graceful fallback (spec §12 ruling 2): `isolate=False` runs
     `run_pipeline` directly in this process. Explicit and logged (a WARNING
     naming the caveat), never silent: dev/SQLite/inline deployments that
     cannot or need not pay for a subprocess keep working, but the returned
     `IsolatedRunResult.isolated` is `False` and `peak_rss_mb` is this
     process's own contaminated high-water mark, not a clean per-job sample
     -- callers (the B5 telemetry loop, once wired) must not fold it into
     `k_path` recalibration.

Deliberately NOT in Sprint 1a-part-1 scope: `sink`, `source_loader`,
`vault_writer`, `registry`, and a non-None `derive_key` are all
callables/objects that cannot cross the process boundary as JSON; passing a
non-default value for any of them raises `ValueError` before a subprocess is
even spawned (fail fast, not a doomed child). Sprint 1a-part-2 (platform
`queue_worker` wiring) and any later widening are separate follow-ups.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.execution._isolated_common import (
    CAPPED_ENV,
    RLIMIT_KINDS,
    IsolatedRunResult,
    classify_abnormal_exit,
    is_memory_failure,
    peak_rss_mb,
)
from decoy_engine.execution._pipeline import run_pipeline

__all__ = ["run_pipeline_isolated"]

_logger = logging.getLogger(__name__)

_WORKER_MODULE = "decoy_engine.execution._isolated_worker"

# kwargs that carry a callable or live object and therefore cannot cross the
# process boundary as JSON. None (their shared default) is fine -- it means
# "let the child resolve its own default," which is exactly what happens
# in-process today. See module docstring's "Deliberately NOT in scope" note.
_NOT_SERIALIZABLE_KWARGS: tuple[str, ...] = (
    "sink",
    "source_loader",
    "vault_writer",
    "registry",
    "derive_key",
)


def run_pipeline_isolated(
    config: dict[str, Any],
    sources: dict[str, pa.Table] | None = None,
    *,
    mem_cap_bytes: int | None = None,
    rlimit_kind: str = "data",
    staging_dir: str | Path | None = None,
    output_dir: str | Path | None = None,
    isolate: bool = True,
    on_spawn: Callable[[int], None] | None = None,
    timeout_s: float | None = None,
    **run_pipeline_kwargs: Any,
) -> IsolatedRunResult:
    """Run one pipeline job in a fresh child process; return a clean result.

    Args:
        config: same as `run_pipeline`'s `config` -- the validated
            `PipelineConfig.model_validate(raw).model_dump()` dict. Must be
            JSON-serializable (it already is, by construction, for every
            caller in this codebase).
        sources: same as `run_pipeline`'s `sources`. Serialized to Parquet
            in a temp directory for the child to read back.
        mem_cap_bytes: if given, a hard `resource.setrlimit` ceiling applied
            IN THE CHILD before it loads sources or calls `run_pipeline`
            (see `_isolated_worker`). `None` (default) applies no cap.
        rlimit_kind: `"data"` (default, brk + anonymous mmaps) or `"as"`
            (total address space) -- see `_isolated_common.RLIMIT_KINDS`.
        staging_dir: scratch root for the payload + (when `output_dir` is
            not given) staged outputs. A fresh `tempfile.mkdtemp` when
            omitted, cleaned up on return.
        output_dir: if given, the final target directory staged outputs are
            atomically committed to on a `completed` outcome only. Never
            created or touched on any other outcome (the whole point of the
            staging contract -- see module docstring point 4).
        isolate: `True` (default) spawns the subprocess. `False` runs
            in-process (module docstring point 5); logs a warning naming
            the governor/telemetry gap.
        on_spawn: called with the child's PID immediately after it is
            spawned, while the run is still in flight. No-op when
            `isolate=False` (there is no child).
        timeout_s: optional wall-clock bound; a child that outlives it is
            killed and classified `crashed` with a timeout diagnostic.
        **run_pipeline_kwargs: forwarded to `run_pipeline` (e.g.
            `engine_version`, `execution_mode`, `substrate`). See
            `_NOT_SERIALIZABLE_KWARGS` for the excluded subset.

    Returns:
        An `IsolatedRunResult`. See its docstring for the field contract
        Sprint 1a-part-2 will consume.

    Raises:
        ValueError: a non-None `sink`/`source_loader`/`vault_writer`/
            `registry`/`derive_key` was passed, `rlimit_kind` is not a
            known kind, or `config`/`run_pipeline_kwargs` is not
            JSON-serializable. Always raised BEFORE any subprocess is
            spawned.
    """
    _reject_unserializable_kwargs(run_pipeline_kwargs)
    if mem_cap_bytes is not None and rlimit_kind not in RLIMIT_KINDS:
        raise ValueError(f"rlimit_kind={rlimit_kind!r} is not one of {sorted(RLIMIT_KINDS)}")

    if not isolate:
        _logger.warning(
            "run_pipeline_isolated(isolate=False): running in-process -- "
            "no governor, no per-job peak, no fresh-execve HWM isolation "
            '(spec §12 ruling 2, "option c"). peak_rss_mb below is this '
            "process's own contaminated high-water mark, not a clean "
            "per-job sample; do not feed it into telemetry recalibration."
        )
        return _run_in_process(config, sources, run_pipeline_kwargs)

    return _run_isolated(
        config,
        sources,
        mem_cap_bytes=mem_cap_bytes,
        rlimit_kind=rlimit_kind,
        staging_dir=staging_dir,
        output_dir=output_dir,
        on_spawn=on_spawn,
        timeout_s=timeout_s,
        run_pipeline_kwargs=run_pipeline_kwargs,
    )


def _reject_unserializable_kwargs(kwargs: dict[str, Any]) -> None:
    offenders = [name for name in _NOT_SERIALIZABLE_KWARGS if kwargs.get(name) is not None]
    if offenders:
        raise ValueError(
            f"run_pipeline_isolated cannot carry a non-None "
            f"{', '.join(offenders)} kwarg across the process boundary "
            f"(Sprint 1a-part-1 scope: only JSON-serializable run_pipeline "
            f"kwargs cross the fork). Leave at the default (None), or call "
            f"run_pipeline directly in-process."
        )


def _run_in_process(
    config: dict[str, Any],
    sources: dict[str, pa.Table] | None,
    run_pipeline_kwargs: dict[str, Any],
) -> IsolatedRunResult:
    try:
        result = run_pipeline(config, sources, **run_pipeline_kwargs)
    except BaseException as exc:
        outcome = "oom_killed" if is_memory_failure(exc) else "crashed"
        return IsolatedRunResult(
            outcome=outcome,
            peak_rss_mb=round(peak_rss_mb(), 1),
            outputs=None,
            quality_metrics={},
            table_kinds={},
            returncode=None,
            signal_number=None,
            error=f"{type(exc).__name__}: {exc}",
            isolated=False,
        )
    return IsolatedRunResult(
        outcome="completed",
        peak_rss_mb=round(peak_rss_mb(), 1),
        outputs=dict(result.outputs),
        quality_metrics=dict(result.quality_metrics),
        table_kinds=dict(result.table_kinds),
        returncode=0,
        signal_number=None,
        error=None,
        isolated=False,
    )


def _run_isolated(
    config: dict[str, Any],
    sources: dict[str, pa.Table] | None,
    *,
    mem_cap_bytes: int | None,
    rlimit_kind: str,
    staging_dir: str | Path | None,
    output_dir: str | Path | None,
    on_spawn: Callable[[int], None] | None,
    timeout_s: float | None,
    run_pipeline_kwargs: dict[str, Any],
) -> IsolatedRunResult:
    owns_work_root = staging_dir is None
    work_root = (
        Path(staging_dir)
        if staging_dir is not None
        else Path(tempfile.mkdtemp(prefix="decoy-isolated-run-"))
    )
    work_root.mkdir(parents=True, exist_ok=True)
    output_dir_path = Path(output_dir) if output_dir is not None else None
    staging_output_dir = _resolve_staging_output_dir(work_root, output_dir_path)

    try:
        payload_path = _write_payload(
            work_root,
            config,
            sources,
            run_pipeline_kwargs,
            mem_cap_bytes,
            rlimit_kind,
            staging_output_dir,
        )
        return _spawn_and_classify(
            payload_path,
            staging_output_dir,
            output_dir_path,
            on_spawn=on_spawn,
            timeout_s=timeout_s,
        )
    finally:
        if owns_work_root:
            shutil.rmtree(work_root, ignore_errors=True)


def _resolve_staging_output_dir(work_root: Path, output_dir: Path | None) -> Path:
    """Where the child stages outputs before commit.

    When `output_dir` is given, staging must share its filesystem so the
    commit step's `os.replace` is a single atomic rename (never a copy) --
    the same same-parent-directory discipline `ParquetTransactionalSink`
    uses for exactly this reason. Without an `output_dir` there is nothing
    to rename into, so staging just lives under this run's own scratch root.
    """
    if output_dir is not None:
        token = uuid.uuid4().hex[:12]
        return output_dir.parent / f"_decoy_isolated_stage_{token}"
    return work_root / "staging_output"


def _write_payload(
    work_root: Path,
    config: dict[str, Any],
    sources: dict[str, pa.Table] | None,
    run_pipeline_kwargs: dict[str, Any],
    mem_cap_bytes: int | None,
    rlimit_kind: str,
    staging_output_dir: Path,
) -> Path:
    sources_dir = work_root / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    sources_manifest: dict[str, str] = {}
    for name, table in (sources or {}).items():
        dest = sources_dir / f"{name}.parquet"
        pq.write_table(table, dest)
        sources_manifest[name] = str(dest)

    payload = {
        "config": config,
        "sources": sources_manifest,
        "kwargs": run_pipeline_kwargs,
        "mem_cap_bytes": mem_cap_bytes,
        "rlimit_kind": rlimit_kind,
        "staging_output_dir": str(staging_output_dir),
    }
    payload_path = work_root / "payload.json"
    try:
        serialized = json.dumps(payload)
    except TypeError as exc:
        raise ValueError(
            f"config / run_pipeline_kwargs must be JSON-serializable for isolated execution: {exc}"
        ) from exc
    payload_path.write_text(serialized, encoding="utf-8")
    return payload_path


def _spawn_and_classify(
    payload_path: Path,
    staging_output_dir: Path,
    output_dir: Path | None,
    *,
    on_spawn: Callable[[int], None] | None,
    timeout_s: float | None,
) -> IsolatedRunResult:
    cmd = [sys.executable, "-m", _WORKER_MODULE, str(payload_path)]
    # _CAPPED_ENV must land BEFORE the child interpreter starts: glibc reads
    # MALLOC_ARENA_MAX at startup and pyarrow reads ARROW_DEFAULT_MEMORY_POOL
    # at import, so `env=` at spawn time is the only place this can work.
    env = {**os.environ, **CAPPED_ENV}
    proc = subprocess.Popen(  # noqa: S603
        cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    if on_spawn is not None:
        # The child's PID is live NOW, mid-run -- this is the hook a governor
        # (or a test simulating one) uses to SIGKILL it before it can report.
        on_spawn(proc.pid)

    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        shutil.rmtree(staging_output_dir, ignore_errors=True)
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
        )

    envelope = _parse_envelope(stdout)
    if (
        proc.returncode == 0
        and envelope is not None
        and envelope.get("outcome")
        in (
            "completed",
            "oom_killed",
            "crashed",
        )
    ):
        return _result_from_envelope(envelope, output_dir, proc.pid)

    # The child died too hard to self-report (a harder rlimit trip the
    # kernel turned into a signal, or an external SIGKILL -- the governor
    # simulation teeth). Classify from the outside; nothing was ever
    # committed to output_dir, so discard whatever the child may have
    # started staging.
    shutil.rmtree(staging_output_dir, ignore_errors=True)
    outcome = classify_abnormal_exit(proc.returncode, stderr)
    signal_number = -proc.returncode if proc.returncode < 0 else None
    signal_name = signal.Signals(signal_number).name if signal_number else None
    stderr_tail = stderr.strip().splitlines()[-1][:300] if stderr.strip() else ""
    return IsolatedRunResult(
        outcome=outcome,
        peak_rss_mb=None,  # unrecoverable: the child is gone and never reported
        outputs=None,
        quality_metrics={},
        table_kinds={},
        returncode=proc.returncode,
        signal_number=signal_number,
        error=(
            f"child terminated abnormally (returncode={proc.returncode}"
            f"{f', signal={signal_name}' if signal_name else ''}); "
            f"stderr tail: {stderr_tail!r}"
        ),
        isolated=True,
        pid=proc.pid,
    )


def _parse_envelope(stdout: str) -> dict[str, Any] | None:
    line = stdout.strip().splitlines()[-1] if stdout.strip() else ""
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def _result_from_envelope(
    envelope: dict[str, Any], output_dir: Path | None, pid: int
) -> IsolatedRunResult:
    outcome = envelope["outcome"]
    peak = envelope.get("peak_rss_mb")
    if outcome != "completed":
        return IsolatedRunResult(
            outcome=outcome,
            peak_rss_mb=peak,
            outputs=None,
            quality_metrics={},
            table_kinds={},
            returncode=0,
            signal_number=None,
            error=envelope.get("error"),
            isolated=True,
            pid=pid,
        )

    outputs, committed_dir = _commit(envelope, output_dir)
    return IsolatedRunResult(
        outcome="completed",
        peak_rss_mb=peak,
        outputs=outputs,
        quality_metrics=envelope.get("quality_metrics") or {},
        table_kinds=envelope.get("table_kinds") or {},
        returncode=0,
        signal_number=None,
        error=None,
        isolated=True,
        pid=pid,
        committed_output_dir=committed_dir,
    )


def _commit(
    envelope: dict[str, Any], output_dir: Path | None
) -> tuple[dict[str, pa.Table], str | None]:
    """Read the child's staged tables, then commit-or-discard per §12 ruling 3.

    Reading back BEFORE the rename (rather than after) means the returned
    `outputs` are correct even in the no-`output_dir` case, where there is
    nothing to rename into and the staging directory is simply discarded
    once read.
    """
    staging_output_dir = Path(envelope["staging_output_dir"])
    outputs = {
        table: pq.read_table(staging_output_dir / f"{table}.parquet")
        for table in envelope.get("staged_tables", [])
    }
    if output_dir is None:
        shutil.rmtree(staging_output_dir, ignore_errors=True)
        return outputs, None

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    # Single atomic rename: staging becomes the target, same guarantee
    # ParquetTransactionalSink.commit documents (POSIX rename(2), atomic
    # within one filesystem -- guaranteed here because
    # _resolve_staging_output_dir rooted staging at output_dir.parent).
    os.replace(staging_output_dir, output_dir)
    return outputs, str(output_dir)

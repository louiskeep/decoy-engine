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
  4. Staging + publication (spec §12 ruling 3, F22 plan 5.7): the child
     writes its output tables into a `publication` stage this driver opened
     beside `output_dir`, never into `output_dir` itself. Only when the child
     exits cleanly with a `completed` envelope does this driver admit the
     stage, measure it and bind it through `publish_unit` (`_isolated_commit`),
     with one durable rename. A SIGKILL at any point before that leaves
     nothing at `output_dir`; the stage is discarded. A target holding another
     result, or a stage holding anything the envelope did not name, is a
     classified `crashed` result with the receipt (MED-1), never a raw error.
  5. Graceful fallback (spec §12 ruling 2): `isolate=False` runs
     `run_pipeline` directly in this process. Explicit and logged (a WARNING
     naming the caveat), never silent: dev/SQLite/inline deployments that
     cannot or need not pay for a subprocess keep working, but the returned
     `IsolatedRunResult.isolated` is `False` and `peak_rss_mb` is this
     process's own contaminated high-water mark, not a clean per-job sample
     -- callers (the B5 telemetry loop, once wired) must not fold it into
     `k_path` recalibration. `output_dir` is honored in this mode too
     (dennis review HIGH-2): the fallback publishes through `publish_unit`
     too, so the two modes are equivalent in what lands on disk, not just in
     `outcome`.

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
import subprocess
import sys
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from typing import Any

import pyarrow as pa

from decoy_engine import fileaccess
from decoy_engine.config._subset_run import refuse_unapplied_subset
from decoy_engine.execution._guards import prepare_declared_execution_sources
from decoy_engine.execution._isolated_commit import (
    COMMITTED,
    Publication,
    begin_publication,
    check_request,
    collect,
    failed_publication,
    publish_resident,
    read_envelope,
    row_errors_of,
    stage_sources,
)
from decoy_engine.execution._isolated_commit import signal_name as _signal_name
from decoy_engine.execution._isolated_common import (
    CAPPED_ENV,
    ISOLATED_WORKER_ENV,
    RESULT_FILENAME,
    RLIMIT_KINDS,
    IsolatedRunOutcome,
    IsolatedRunResult,
    abnormal_exit_text,
    classify_abnormal_exit,
    exception_error_text,
    is_memory_failure,
    peak_rss_mb,
)
from decoy_engine.execution._isolated_memwatch import error_suffix, evidence_of, start_sampler
from decoy_engine.execution._isolated_reason import reason_from_envelope
from decoy_engine.execution._pipeline import run_pipeline
from decoy_engine.fileaccess import FileAccess, FileRequest, FileRole, entry_point
from decoy_engine.profile import DeclarationRequest
from decoy_engine.publication import CommitReceipt, PublicationRequest
from decoy_engine.reason_adapter import reason_from_exception
from decoy_engine.reasons import ReasonRecord
from decoy_engine.reasons import decoy as _decoy_reasons

__all__ = ["run_pipeline_isolated"]

_logger = logging.getLogger(__name__)

_WORKER_MODULE = "decoy_engine.execution._isolated_worker"

# MED-2: an unbounded default (`None`) means a hung child (a wedged
# provider, a deadlocked DuckDB query) blocks its caller forever with no
# governor wired yet -- Sprint 1a-part-1 ships before B4's real governor, so
# this is the unconditional safety bound in the meantime, not a tuned
# per-job budget. 30 minutes is generous enough not to false-positive on a
# legitimately large synth job while still guaranteeing SOME bound exists.
# Callers with a tighter or looser real budget should pass their own
# `timeout_s` explicitly; this is only the "no config at all" floor.
_DEFAULT_TIMEOUT_S = 1800.0

# kwargs that carry a callable or live object and therefore cannot cross the
# process boundary as JSON. None (their shared default) is fine -- it means
# "let the child resolve its own default," which is exactly what happens
# in-process today. See module docstring's "Deliberately NOT in scope" note.
_NOT_SERIALIZABLE_KWARGS: tuple[str, ...] = (
    "sink",
    "progress",
    "source_loader",
    "vault_writer",
    "registry",
    "derive_key",
)


@entry_point
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
    timeout_s: float | None = _DEFAULT_TIMEOUT_S,
    file_access: FileAccess | None = None,
    request: PublicationRequest | None = None,
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
            not given) staged outputs. A fresh protected directory when
            omitted, cleaned up on return; one you name is created or checked
            owner-only (a symlink or a foreign-owned directory is refused).
        output_dir: if given, the target the staged outputs are published to
            through `publish_unit` (F22) on a `completed` outcome only; the
            receipt is `IsolatedRunResult.publication_receipt`. Never created or
            touched on any other outcome (module docstring point 4).
        isolate: `True` (default) spawns the subprocess. `False` runs
            in-process (module docstring point 5); logs a warning naming
            the governor/telemetry gap.
        on_spawn: called with the child's PID immediately after it is
            spawned, while the run is still in flight. No-op when
            `isolate=False` (there is no child).
        timeout_s: wall-clock bound; a child that outlives it is killed and
            classified `crashed` with a timeout diagnostic. Defaults to
            `_DEFAULT_TIMEOUT_S` (30 minutes, MED-2) -- an unconditional
            safety bound, not a tuned per-job budget; pass `None` only if
            you have deliberately decided an unbounded hang is acceptable.
        **run_pipeline_kwargs: forwarded to `run_pipeline` (e.g.
            `engine_version`, `execution_mode`, `native_threads`). See
            `_NOT_SERIALIZABLE_KWARGS` for the excluded subset.

        file_access: the file-access policy. A child process cannot inherit a policy and
            the child's config-named reads cannot be listed in advance, so under any policy
            but the ambient CLI one the route runs in-process only and takes no
            `staging_dir` or `output_dir` path (see "Raises").
        request: the publication request for `output_dir`; a retry passes the same
            `operation_id`. Without one the run is its own attempt (a fresh id).

    Returns:
        An `IsolatedRunResult`. See its docstring for the field contract
        Sprint 1a-part-2 will consume.

    Raises:
        PipelineValidationError: `config` sets `subset:` (raised before staging or spawn).
        FileAccessUnavailable: a strict policy is installed and the call would spawn a
            child, or names a `staging_dir` or `output_dir` path. Raised before any I/O.
        ValueError: a non-None `sink`/`source_loader`/`vault_writer`/
            `registry`/`derive_key` was passed, `rlimit_kind` is not a
            known kind, `request` carries a vault, or `config`/`run_pipeline_kwargs` is not
            JSON-serializable. Always raised BEFORE any subprocess is
            spawned.
    """
    refuse_unapplied_subset(config)
    _reject_unserializable_kwargs(run_pipeline_kwargs)
    if mem_cap_bytes is not None and rlimit_kind not in RLIMIT_KINDS:
        raise ValueError(f"rlimit_kind={rlimit_kind!r} is not one of {sorted(RLIMIT_KINDS)}")
    check_request(request)
    _require_ambient_for_paths(
        spawns=isolate and os.environ.get(ISOLATED_WORKER_ENV) != "1",
        names_paths=staging_dir is not None or output_dir is not None,
    )

    # Before any staging or spawn: the child rechecks, since a capability cannot cross the process.
    prepare_declared_execution_sources(
        DeclarationRequest.for_run(config, {n: t.schema for n, t in (sources or {}).items()}),
        file_access=file_access,
    )

    # Re-entry guard: a child this primitive spawns carries ISOLATED_WORKER_ENV
    # (see `_spawn_and_classify`). If `run_pipeline`'s routing layer inside that
    # child re-enters here (probe/governor), spawning again would start a self-
    # multiplying subprocess chain that saturates the host. Degrade to in-process
    # instead -- the re-entrant probe then reports a non-isolated measurement and
    # routing falls back to the in-process estimator, so the chain stops at
    # depth 1 (marker's docstring in `_isolated_common` has the full account).
    already_isolated = isolate and os.environ.get(ISOLATED_WORKER_ENV) == "1"
    if not isolate or already_isolated:
        if already_isolated:
            _logger.warning(
                "run_pipeline_isolated(isolate=True) re-entered from inside an "
                "isolated worker (%s set): running in-process to prevent a self-"
                "multiplying subprocess chain. peak_rss_mb below is this process's "
                "own contaminated high-water mark, not a clean per-job sample.",
                ISOLATED_WORKER_ENV,
            )
        else:
            _logger.warning(
                "run_pipeline_isolated(isolate=False): running in-process -- "
                "no governor, no per-job peak, no fresh-execve HWM isolation "
                '(spec §12 ruling 2, "option c"). peak_rss_mb below is this '
                "process's own contaminated high-water mark, not a clean "
                "per-job sample; do not feed it into telemetry recalibration."
            )
        output_dir_path = Path(output_dir) if output_dir is not None else None
        return _run_in_process(config, sources, run_pipeline_kwargs, output_dir_path, request)

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
        request=request,
    )


def _require_ambient_for_paths(*, spawns: bool, names_paths: bool) -> None:
    """Under a strict policy this route can carry neither a child process nor a caller path.

    The child starts a fresh interpreter that inherits no policy, and which files its
    config-named reads open is not known to the parent in advance, so a capability cannot be
    handed across. `staging_dir` and `output_dir` are caller paths this module's own
    staging and commit operate on. Refusing here, before any I/O, is the fail-closed answer."""
    if spawns or names_paths:
        fileaccess.require_ambient(
            "run_pipeline_isolated, which would spawn a child process or name a staging or "
            "output path,"
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
    output_dir: Path | None,
    request: PublicationRequest | None = None,
) -> IsolatedRunResult:
    try:
        result = run_pipeline(config, sources, **run_pipeline_kwargs)
    except BaseException as exc:
        outcome: IsolatedRunOutcome = "oom_killed" if is_memory_failure(exc) else "crashed"
        return IsolatedRunResult(
            outcome=outcome,
            peak_rss_mb=round(peak_rss_mb(), 1),
            outputs=None,
            quality_metrics={},
            table_kinds={},
            returncode=None,
            signal_number=None,
            error=exception_error_text(exc),
            isolated=False,
            reason=reason_from_exception(exc),
        )

    receipt: CommitReceipt | None = None
    if output_dir is not None:
        # HIGH-2: honor output_dir in the fallback too, through the same publication as the
        # isolated path, so isolate=True/False never diverge on what lands on disk.
        receipt, stage = publish_resident(output_dir, result.outputs, request)
        if receipt.outcome not in COMMITTED:
            fields = {"peak_rss_mb": round(peak_rss_mb(), 1), "returncode": None}
            return failed_publication(receipt, stage, isolated=False, **fields)

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
        committed_output_dir=None if receipt is None else str(output_dir),
        row_errors=tuple(result.row_errors),
        publication_receipt=receipt,
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
    request: PublicationRequest | None = None,
) -> IsolatedRunResult:
    scratch = ExitStack()  # removes an engine-made work root; a caller-named one is left alone
    work_root = (
        fileaccess.open_protected_dir(staging_dir, create=True)
        if staging_dir is not None
        else scratch.enter_context(fileaccess.protected_scratch_dir(prefix="decoy-isolated-run-"))
    )
    pub: Publication | CommitReceipt | None = None
    try:
        pub = begin_publication(Path(output_dir), request) if output_dir is not None else None
        if isinstance(pub, CommitReceipt):
            return failed_publication(pub, None, peak_rss_mb=None, returncode=None, isolated=True)
        # The child stages into the publication stage beside `output_dir`, or into scratch.
        staging_output_dir = pub.stage if pub is not None else work_root / "staging_output"
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
            pub,
            on_spawn=on_spawn,
            timeout_s=timeout_s,
            mem_cap_bytes=mem_cap_bytes,
            rlimit_kind=rlimit_kind,
        )
    finally:
        if isinstance(pub, Publication):
            pub.txn.abort()  # a no-op once bound
        scratch.close()


def _write_payload(
    work_root: Path,
    config: dict[str, Any],
    sources: dict[str, pa.Table] | None,
    run_pipeline_kwargs: dict[str, Any],
    mem_cap_bytes: int | None,
    rlimit_kind: str,
    staging_output_dir: Path,
) -> Path:
    sources_manifest = stage_sources(work_root, sources or {})

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
    # The payload may carry DSNs with credentials: write_private makes it 0600 at
    # creation (no umask window, no late chmod) and publishes it atomically.
    fileaccess.write_private(
        FileRequest(FileRole.CONFIG, "write", str(payload_path), "isolated.payload"),
        serialized.encode("utf-8"),
    )
    return payload_path


def _spawn_and_classify(
    payload_path: Path,
    staging_output_dir: Path,
    pub: Publication | None,
    *,
    on_spawn: Callable[[int], None] | None,
    timeout_s: float | None,
    mem_cap_bytes: int | None,
    rlimit_kind: str,
) -> IsolatedRunResult:
    # HIGH-1: the envelope is a known FILE in work_root (payload_path's own
    # parent), never stdout's last line -- see `_isolated_common.RESULT_
    # FILENAME`'s docstring for the stdout-contamination class this
    # replaces. stdout/stderr below remain captured for diagnostics only.
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
            # it can report. MED-3: this call is now INSIDE the try, so a
            # raising callback still hits the `except BaseException` below
            # and gets the child killed + reaped rather than leaked.
            on_spawn(proc.pid)
        stdout, stderr = proc.communicate(timeout=timeout_s)
        # Stop before envelope work so late parsing cannot age or extend the evidence.
        finished = sampler.finish() if sampler else None
    except subprocess.TimeoutExpired:
        proc.kill()
        _stdout, _stderr = proc.communicate()
        finished = sampler.finish() if sampler else None
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
            memory_evidence=evidence_of(finished, abnormal=False),
            reason=ReasonRecord.for_reason(_decoy_reasons.ISOLATED_RUN_TIMED_OUT),
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
        result = _result_from_envelope(envelope, pub, proc.pid, staging_output_dir)
        return replace(result, memory_evidence=evidence_of(finished, abnormal=False))

    # The child died too hard to self-report (a harder rlimit trip the
    # kernel turned into a signal, or an external SIGKILL -- the governor
    # simulation teeth). Classify from the outside; nothing was ever
    # committed to output_dir, so discard whatever the child may have
    # started staging.
    shutil.rmtree(staging_output_dir, ignore_errors=True)
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
        reason=ReasonRecord.for_reason(
            _decoy_reasons.ISOLATED_WORKER_OUT_OF_MEMORY
            if outcome == "oom_killed"
            else _decoy_reasons.ISOLATED_WORKER_DIED
        ),
    )


def _result_from_envelope(
    envelope: dict[str, Any], pub: Publication | None, pid: int, staging_output_dir: Path
) -> IsolatedRunResult:
    outcome = envelope["outcome"]
    peak = envelope.get("peak_rss_mb")
    if outcome != "completed":
        # A self-reported failure may have staged part of its output; nothing commits it.
        shutil.rmtree(staging_output_dir, ignore_errors=True)
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
            reason=reason_from_envelope(envelope),
        )

    # Read back before publishing, so `outputs` is right with or without an `output_dir`.
    collected = collect(envelope, staging_output_dir, pub)
    if pub is None:
        shutil.rmtree(staging_output_dir, ignore_errors=True)  # nothing to publish it to
    if isinstance(collected, CommitReceipt):
        # MED-1: a refused stage or a publication that did not commit is a classified result.
        fields = {"peak_rss_mb": peak, "returncode": 0, "isolated": True, "pid": pid}
        return failed_publication(collected, staging_output_dir, **fields)
    outputs, receipt = collected
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
        committed_output_dir=None if pub is None else str(pub.target),
        row_errors=row_errors_of(envelope),
        publication_receipt=receipt,
    )

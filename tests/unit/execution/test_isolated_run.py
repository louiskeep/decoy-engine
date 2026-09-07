"""Sprint 1a-part-1: `run_pipeline_isolated` (the process-model prerequisite).

`docs/plans/2026-07-10-oom-avoidance-routing-redesign.md` §12 (RATIFIED):
port `scripts/fk_memory_probe.py`'s fresh-`execve`-per-job isolation from
benchmark harness into a production execution primitive. These are the four
teeth the spec calls out by name:

  1. A job run under a deliberately-low `mem_cap_bytes` OOMs -> classified
     `oom_killed` with a clean diagnostic, not an opaque uncaught crash.
  2. HWM-contamination guard: a large PARENT allocation must not leak into
     the CHILD's reported `peak_rss_mb` -- the whole reason for fresh
     `execve` over a warm worker (fk_memory_probe's own documented
     `ru_maxrss`-vs-VmHWM finding).
  3. A normal small job completes with correct artifacts and a plausible
     peak.
  4. Killing the child mid-run (simulating the governor's SIGKILL) leaves
     NO partial output at the final target path -- staging is discarded,
     never committed.

Plus light coverage of the graceful in-process fallback (spec §12 ruling 2)
and the cross-process-boundary kwarg guard, both part of "what to build" but
not named teeth.
"""

from __future__ import annotations

import glob
import inspect
import os
import signal
import stat
import threading
import time
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.execution import IsolatedRunResult, run_pipeline_isolated

_ENGINE_VERSION = "isolated-run-test"

# --------------------------------------------------------------------------
# Fake-worker fixture support: several dennis-review teeth need to control
# exactly what the CHILD process does (print stray stdout, hang forever,
# write a corrupt/missing envelope) independent of the real worker's own
# behavior -- injecting that into `_isolated_worker.py` itself would pollute
# production code with test-only branches. Instead we point `_WORKER_MODULE`
# at a small script dropped on the child's import path for the duration of
# one test, matching pytest's normal monkeypatch-and-restore discipline.
# --------------------------------------------------------------------------


def _install_fake_worker(tmp_path, monkeypatch, module_name: str, source: str) -> None:
    fake_dir = tmp_path / "_fake_workers"
    fake_dir.mkdir(exist_ok=True)
    (fake_dir / f"{module_name}.py").write_text(source, encoding="utf-8")
    existing = os.environ.get("PYTHONPATH", "")
    new_path = str(fake_dir) + (os.pathsep + existing if existing else "")
    monkeypatch.setenv("PYTHONPATH", new_path)
    monkeypatch.setattr("decoy_engine.execution._isolated_run._WORKER_MODULE", module_name)


_HANGING_WORKER_SRC = """
import time
time.sleep(999)
"""

_SILENT_EXIT_WORKER_SRC = """
# Exits immediately without writing a result envelope or printing anything --
# simulates a child that vanished before it could self-report.
"""

_CORRUPT_ENVELOPE_WORKER_SRC = """
import sys
from pathlib import Path

result_path = Path(sys.argv[1]).parent / "result.json"
result_path.write_text("{not valid json!!", encoding="utf-8")
"""

_NON_MEMORY_CRASH_WORKER_SRC = """
import sys

sys.stderr.write(
    "Traceback (most recent call last):\\n"
    "  File \\"worker.py\\", line 1, in <module>\\n"
    "ValueError: definitely not a memory problem\\n"
)
sys.exit(1)
"""

_NOISY_WORKER_SRC = """
import json
import sys
from pathlib import Path

import pyarrow.parquet as pq

from decoy_engine.execution._pipeline import run_pipeline

# Simulates stray child stdout BEFORE the envelope is written -- an atexit
# handler, BLAS/OpenMP teardown chatter, or a chatty provider.
print("stray stdout BEFORE the envelope")

payload_path = Path(sys.argv[1])
with open(payload_path, encoding="utf-8") as fh:
    payload = json.load(fh)

sources = {name: pq.read_table(path) for name, path in (payload.get("sources") or {}).items()}
result = run_pipeline(payload["config"], sources, **payload["kwargs"])

staging_output_dir = Path(payload["staging_output_dir"])
staging_output_dir.mkdir(parents=True, exist_ok=True)
staged_tables = []
for table, data in result.outputs.items():
    pq.write_table(data, staging_output_dir / f"{table}.parquet")
    staged_tables.append(table)

envelope = {
    "outcome": "completed",
    "peak_rss_mb": 1.0,
    "staging_output_dir": str(staging_output_dir),
    "staged_tables": staged_tables,
    "quality_metrics": dict(result.quality_metrics),
    "table_kinds": dict(result.table_kinds),
}
result_path = payload_path.parent / "result.json"
result_path.write_text(json.dumps(envelope, default=str), encoding="utf-8")

# Simulates stray child stdout AFTER the envelope is written -- a user print
# inside a strategy, or late-firing teardown output.
print("stray stdout AFTER the envelope")
"""


def _validated_dump(cfg: dict) -> dict:
    return PipelineConfig.model_validate(cfg).model_dump()


def _mask_config(tmp_path, n_cols: int) -> dict:
    """A pure-mask, no-FK config: `redact` needs no Faker/network work, so
    the job's wall-clock cost is dominated by DataFrame construction and the
    boundary conversion -- exactly the allocation-heavy, CPU-light shape
    that makes a low `mem_cap_bytes` trip reliably without a slow test."""
    return _validated_dump(
        {
            "version": 1,
            "global_settings": {"seed": 42},
            "sources": {
                "customers": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "customers.csv"),
                },
            },
            "tables": [
                {
                    "name": "customers",
                    "columns": [{"name": f"col{i}", "strategy": "redact"} for i in range(n_cols)],
                },
            ],
            "targets": {
                "customers": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "out.csv"),
                },
            },
        }
    )


def _mask_sources(tmp_path, n_rows: int, n_cols: int) -> dict[str, pa.Table]:
    data = {f"col{i}": [f"value-{i}-{row}" for row in range(n_rows)] for i in range(n_cols)}
    df = pd.DataFrame(data)
    df.to_csv(tmp_path / "customers.csv", index=False)
    return {"customers": pa.Table.from_pandas(df, preserve_index=False)}


def _row_error_config(tmp_path) -> dict:
    """bucketize on a column with one non-numeric non-null cell -> a
    `format_error` row error (MED-4 fixture)."""
    return _validated_dump(
        {
            "version": 1,
            "global_settings": {"seed": 42},
            "sources": {
                "customers": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "customers.csv"),
                },
            },
            "tables": [
                {
                    "name": "customers",
                    "columns": [
                        {"name": "age", "strategy": "bucketize", "provider_config": {"width": 10}},
                    ],
                },
            ],
            "targets": {
                "customers": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "out.csv"),
                },
            },
            # Quarantine must be enabled for the row-error's trigger, or
            # run_pipeline fails the whole job closed (RowErrorsFailedError)
            # instead of completing with the bad row routed aside -- this
            # fixture wants a `completed` outcome carrying a non-empty
            # row_errors, not a failed job.
            "quarantine": {
                "enabled": True,
                "output_path": str(tmp_path / "quarantine.jsonl"),
                "triggers": ["format_error"],
            },
        }
    )


def _row_error_sources(tmp_path) -> dict[str, pa.Table]:
    df = pd.DataFrame({"age": ["23", "not-a-number", "47"]})
    df.to_csv(tmp_path / "customers.csv", index=False)
    return {"customers": pa.Table.from_pandas(df, preserve_index=False)}


# --------------------------------------------------------------------------
# Teeth 3: normal small job completes cleanly
# --------------------------------------------------------------------------


class TestNormalJobCompletes:
    def test_small_job_completes_with_correct_artifacts_and_plausible_peak(self, tmp_path):
        cfg = _mask_config(tmp_path, n_cols=2)
        sources = _mask_sources(tmp_path, n_rows=50, n_cols=2)

        result = run_pipeline_isolated(cfg, sources, engine_version=_ENGINE_VERSION)

        assert isinstance(result, IsolatedRunResult)
        assert result.outcome == "completed"
        assert result.error is None
        assert result.isolated is True
        assert result.outputs is not None
        assert "customers" in result.outputs
        assert result.outputs["customers"].num_rows == 50
        # row-level content check is a light correctness signal, not a full
        # parity suite -- just confirms real masking happened.
        assert set(result.outputs["customers"].column("col0").to_pylist()) == {"REDACTED"}
        assert result.table_kinds == {"customers": "mask"}
        # A plausible peak: positive, and well under a sanity ceiling (a
        # process that imports pandas/pyarrow/duckdb for a 50-row job should
        # never approach a GB).
        assert result.peak_rss_mb is not None
        assert 0 < result.peak_rss_mb < 3000

    def test_disallowed_kwargs_reject_before_spawning_a_subprocess(self, tmp_path):
        """sink/source_loader/vault_writer/registry/derive_key cannot cross
        the process boundary (they are callables/objects, not JSON). This
        must fail BEFORE a subprocess is spawned, not inside a doomed child."""
        cfg = _mask_config(tmp_path, n_cols=1)
        sources = _mask_sources(tmp_path, n_rows=5, n_cols=1)

        with pytest.raises(ValueError, match="sink"):
            run_pipeline_isolated(
                cfg,
                sources,
                engine_version=_ENGINE_VERSION,
                sink=object(),
            )


# --------------------------------------------------------------------------
# Teeth 1: low mem_cap_bytes -> clean oom_killed classification
# --------------------------------------------------------------------------


class TestMemCapOom:
    def test_low_mem_cap_classifies_oom_cleanly_not_an_opaque_crash(self, tmp_path):
        # The cap sits ABOVE the child's import floor on purpose. A worker that
        # imports duckdb + polars reserves close to half a GB of RLIMIT_DATA in
        # glibc arenas before any job runs (RSS is far lower; the arenas are the
        # ceiling that RLIMIT_DATA sees). A cap below that floor kills the child
        # mid-import, and whether that death is a caught MemoryError (soft,
        # self-reported oom_killed) or a hard C-extension abort (classified
        # crashed) depends on the runner's glibc, so it flakes. Capping above
        # the floor and handing the job enough rows to exhaust the remaining
        # headroom forces the OOM at RUN time, where run_pipeline raises a
        # catchable ArrowMemoryError the worker self-reports. That is the real
        # guarantee under test: a running job that exhausts its cap is named
        # oom_killed, never an opaque crashed.
        cfg = _mask_config(tmp_path, n_cols=8)
        sources = _mask_sources(tmp_path, n_rows=2_000_000, n_cols=8)

        result = run_pipeline_isolated(
            cfg,
            sources,
            engine_version=_ENGINE_VERSION,
            mem_cap_bytes=768 * 1024 * 1024,
            rlimit_kind="data",
        )

        assert result.outcome == "oom_killed"
        assert result.outputs is None
        # A clean diagnostic: short, names the failure, not a raw multi-KB
        # traceback dump.
        assert result.error is not None
        assert len(result.error) < 600
        assert result.isolated is True


# --------------------------------------------------------------------------
# Teeth 2: HWM-contamination guard
# --------------------------------------------------------------------------


class TestHwmContaminationGuard:
    def test_parent_inflated_peak_does_not_leak_into_child_result(self, tmp_path):
        cfg = _mask_config(tmp_path, n_cols=2)
        sources = _mask_sources(tmp_path, n_rows=50, n_cols=2)

        # Inflate THIS process's RSS well past anything a 50-row job's
        # subprocess should ever report, before running the isolated job.
        # ru_maxrss would leak this into a warm worker's report (the
        # fk_memory_probe finding: a child of a 600 MB parent reported
        # ru_maxrss ~610 MB vs its own VmHWM ~9 MB); a fresh execve must not.
        parent_ballast = bytearray(500 * 1024 * 1024)
        try:
            result = run_pipeline_isolated(cfg, sources, engine_version=_ENGINE_VERSION)
        finally:
            del parent_ballast

        assert result.outcome == "completed"
        assert result.peak_rss_mb is not None
        # Comfortably below the 500 MB parent ballast: the child's own
        # baseline (interpreter + pandas/pyarrow/duckdb imports + a 50-row
        # job) should land at a few hundred MB at most, never near 500.
        assert result.peak_rss_mb < 400


# --------------------------------------------------------------------------
# Teeth 4: mid-run kill leaves no partial output at the final target
# --------------------------------------------------------------------------


class TestGovernorKillLeavesNoPartialOutput:
    def test_sigkill_mid_run_never_commits_to_output_dir(self, tmp_path):
        cfg = _mask_config(tmp_path, n_cols=6)
        # A job with enough real work that the SIGKILL (sent the instant the
        # PID is known) lands mid-run rather than racing a job that would
        # have already finished.
        sources = _mask_sources(tmp_path, n_rows=300_000, n_cols=6)
        output_dir = tmp_path / "final_target"

        killed = threading.Event()

        def _kill_immediately(pid: int) -> None:
            os.kill(pid, signal.SIGKILL)
            killed.set()

        result = run_pipeline_isolated(
            cfg,
            sources,
            engine_version=_ENGINE_VERSION,
            output_dir=output_dir,
            on_spawn=_kill_immediately,
        )

        assert killed.is_set()
        assert result.outcome != "completed"
        assert result.outcome == "oom_killed"  # bare SIGKILL classification (probe convention)
        assert result.committed_output_dir is None
        assert result.outputs is None
        # The load-bearing assertion: nothing was ever committed to the
        # final target path. Staging is discarded, never renamed into it.
        assert not output_dir.exists()


# --------------------------------------------------------------------------
# Graceful in-process fallback (spec §12 ruling 2, "option c")
# --------------------------------------------------------------------------


class TestInProcessFallback:
    def test_isolate_false_runs_in_process_and_labels_itself(self, tmp_path):
        cfg = _mask_config(tmp_path, n_cols=1)
        sources = _mask_sources(tmp_path, n_rows=10, n_cols=1)

        result = run_pipeline_isolated(cfg, sources, engine_version=_ENGINE_VERSION, isolate=False)

        assert result.outcome == "completed"
        assert result.isolated is False
        assert result.outputs is not None
        assert result.outputs["customers"].num_rows == 10


# --------------------------------------------------------------------------
# HIGH-2: isolate=False must honor output_dir with the same atomic-commit
# discipline as the isolated path -- not silently write nothing.
# --------------------------------------------------------------------------


class TestInProcessFallbackHonorsOutputDir:
    def test_isolate_false_honors_output_dir_and_commits_atomically(self, tmp_path):
        cfg = _mask_config(tmp_path, n_cols=1)
        sources = _mask_sources(tmp_path, n_rows=10, n_cols=1)
        output_dir = tmp_path / "final_target"

        result = run_pipeline_isolated(
            cfg,
            sources,
            engine_version=_ENGINE_VERSION,
            isolate=False,
            output_dir=output_dir,
        )

        assert result.outcome == "completed"
        assert result.isolated is False
        assert result.committed_output_dir == str(output_dir)
        assert output_dir.exists()
        assert (output_dir / "customers.parquet").exists()
        # No leftover staging directory next to the committed target.
        assert not list(output_dir.parent.glob("_decoy_isolated_stage_*"))


# --------------------------------------------------------------------------
# HIGH-1: the result envelope is a known FILE, not stdout's last line -- a
# child that prints stray stdout before/after the envelope must not get
# misclassified.
# --------------------------------------------------------------------------


class TestEnvelopeSurvivesStdoutNoise:
    def test_stray_stdout_before_and_after_envelope_still_parses(self, tmp_path, monkeypatch):
        _install_fake_worker(tmp_path, monkeypatch, "noisy_worker", _NOISY_WORKER_SRC)
        cfg = _mask_config(tmp_path, n_cols=2)
        sources = _mask_sources(tmp_path, n_rows=50, n_cols=2)

        result = run_pipeline_isolated(cfg, sources, engine_version=_ENGINE_VERSION)

        assert result.outcome == "completed"
        assert result.error is None
        assert result.outputs is not None
        assert result.outputs["customers"].num_rows == 50


# --------------------------------------------------------------------------
# MED-1: a pre-existing non-empty commit target must classify as a clean
# `crashed` result, not escape as a raw OSError with staging orphaned.
# --------------------------------------------------------------------------


class TestCommitFailureIsClassified:
    @pytest.mark.parametrize("isolate", [True, False])
    def test_pre_existing_non_empty_target_is_classified_not_a_raw_oserror(self, tmp_path, isolate):
        cfg = _mask_config(tmp_path, n_cols=1)
        sources = _mask_sources(tmp_path, n_rows=5, n_cols=1)
        output_dir = tmp_path / "final_target"
        output_dir.mkdir()
        (output_dir / "preexisting.txt").write_text("do not clobber me")

        result = run_pipeline_isolated(
            cfg,
            sources,
            engine_version=_ENGINE_VERSION,
            output_dir=output_dir,
            isolate=isolate,
        )

        assert result.outcome == "crashed"
        assert result.outputs is None
        assert result.committed_output_dir is None
        assert result.error is not None
        # os.replace refused (ENOTEMPTY) -- the pre-existing target must
        # survive completely untouched.
        assert (output_dir / "preexisting.txt").read_text() == "do not clobber me"
        # No orphaned staging directory left outside work_root.
        assert not list(output_dir.parent.glob("_decoy_isolated_stage_*"))


# --------------------------------------------------------------------------
# MED-2: timeout_s must have a bounded default, and a hanging child must
# actually be killed and classified cleanly, not block forever.
# --------------------------------------------------------------------------


class TestTimeoutHasSaneDefault:
    def test_timeout_s_defaults_to_a_bounded_ceiling_not_none(self):
        sig = inspect.signature(run_pipeline_isolated)
        default = sig.parameters["timeout_s"].default
        assert default is not None
        assert default > 0


class TestHangingChildHitsTimeout:
    def test_hanging_child_is_killed_and_classified_cleanly(self, tmp_path, monkeypatch):
        _install_fake_worker(tmp_path, monkeypatch, "hanging_worker", _HANGING_WORKER_SRC)
        cfg = _mask_config(tmp_path, n_cols=1)
        sources = _mask_sources(tmp_path, n_rows=5, n_cols=1)

        start = time.monotonic()
        result = run_pipeline_isolated(cfg, sources, engine_version=_ENGINE_VERSION, timeout_s=0.5)
        elapsed = time.monotonic() - start

        assert result.outcome == "crashed"
        assert result.outputs is None
        assert "timeout" in (result.error or "").lower()
        # Killed promptly -- nowhere near the fake worker's 999s sleep.
        assert elapsed < 15


# --------------------------------------------------------------------------
# MED-3: on_spawn raising must not leak the child process (zombie + fd
# leak) -- it must be killed and reaped before the exception propagates.
# --------------------------------------------------------------------------


class TestOnSpawnExceptionReapsChild:
    def test_on_spawn_raising_does_not_leak_the_child_process(self, tmp_path):
        cfg = _mask_config(tmp_path, n_cols=1)
        sources = _mask_sources(tmp_path, n_rows=5, n_cols=1)
        seen_pid: list[int] = []

        def _raiser(pid: int) -> None:
            seen_pid.append(pid)
            raise RuntimeError("governor callback exploded")

        with pytest.raises(RuntimeError, match="governor callback exploded"):
            run_pipeline_isolated(cfg, sources, engine_version=_ENGINE_VERSION, on_spawn=_raiser)

        assert seen_pid, "on_spawn must have been called with a live pid before raising"
        pid = seen_pid[0]
        # The child must be reaped (no zombie / fd leak) even though the
        # governor callback blew up mid-spawn.
        assert not Path(f"/proc/{pid}").exists()


# --------------------------------------------------------------------------
# MED-4: row_errors (the user-facing quarantine surface) must cross the
# process boundary, unlike timings/warnings.
# --------------------------------------------------------------------------


class TestRowErrorsCarried:
    def test_row_errors_cross_the_process_boundary_isolated(self, tmp_path):
        cfg = _row_error_config(tmp_path)
        sources = _row_error_sources(tmp_path)

        result = run_pipeline_isolated(cfg, sources, engine_version=_ENGINE_VERSION)

        assert result.outcome == "completed"
        assert len(result.row_errors) == 1
        err = result.row_errors[0]
        assert err.table == "customers"
        assert err.column == "age"
        assert err.trigger == "format_error"

    def test_row_errors_carried_in_process_fallback_too(self, tmp_path):
        cfg = _row_error_config(tmp_path)
        sources = _row_error_sources(tmp_path)

        result = run_pipeline_isolated(cfg, sources, engine_version=_ENGINE_VERSION, isolate=False)

        assert result.outcome == "completed"
        assert len(result.row_errors) == 1
        assert result.row_errors[0].trigger == "format_error"


# --------------------------------------------------------------------------
# MED-5a: strengthen tooth 4 with a marker-file handshake -- kill AFTER the
# child has staged output (proving the staged-then-killed commit race), not
# merely at spawn (which could beat the child to any work at all).
# --------------------------------------------------------------------------


class TestGovernorKillAfterStagingLeavesNoPartialOutput:
    def test_sigkill_after_staging_but_before_commit_never_commits(self, tmp_path):
        cfg = _mask_config(tmp_path, n_cols=6)
        sources = _mask_sources(tmp_path, n_rows=300_000, n_cols=6)
        output_dir = tmp_path / "final_target"

        staged_seen = threading.Event()

        def _kill_after_staged(pid: int) -> None:
            deadline = time.monotonic() + 20.0
            staging_glob = str(output_dir.parent / "_decoy_isolated_stage_*")
            while time.monotonic() < deadline and not staged_seen.is_set():
                for match in glob.glob(staging_glob):
                    if list(Path(match).glob("*.parquet")):
                        staged_seen.set()
                        break
                if not staged_seen.is_set():
                    time.sleep(0.01)
            os.kill(pid, signal.SIGKILL)

        result = run_pipeline_isolated(
            cfg,
            sources,
            engine_version=_ENGINE_VERSION,
            output_dir=output_dir,
            on_spawn=_kill_after_staged,
        )

        assert staged_seen.is_set(), (
            "kill fired before the child ever staged output -- this proves "
            "only kill-at-spawn, not the staged-then-killed commit race"
        )
        assert result.outcome != "completed"
        assert result.committed_output_dir is None
        assert not output_dir.exists()
        assert not list(output_dir.parent.glob("_decoy_isolated_stage_*"))


# --------------------------------------------------------------------------
# MED-5b: missing-path coverage -- a missing envelope, a corrupt envelope,
# and a genuine non-memory crash must all classify as `crashed` (not
# `oom_killed`, not a driver-side exception).
# --------------------------------------------------------------------------


class TestMissingOrCorruptEnvelope:
    def test_missing_envelope_classifies_crashed_not_a_driver_exception(
        self, tmp_path, monkeypatch
    ):
        _install_fake_worker(tmp_path, monkeypatch, "silent_exit_worker", _SILENT_EXIT_WORKER_SRC)
        cfg = _mask_config(tmp_path, n_cols=1)
        sources = _mask_sources(tmp_path, n_rows=5, n_cols=1)

        result = run_pipeline_isolated(cfg, sources, engine_version=_ENGINE_VERSION)

        assert result.outcome == "crashed"
        assert result.outputs is None

    def test_corrupt_envelope_classifies_crashed_not_a_driver_exception(
        self, tmp_path, monkeypatch
    ):
        _install_fake_worker(
            tmp_path, monkeypatch, "corrupt_envelope_worker", _CORRUPT_ENVELOPE_WORKER_SRC
        )
        cfg = _mask_config(tmp_path, n_cols=1)
        sources = _mask_sources(tmp_path, n_rows=5, n_cols=1)

        result = run_pipeline_isolated(cfg, sources, engine_version=_ENGINE_VERSION)

        assert result.outcome == "crashed"
        assert result.outputs is None

    def test_non_memory_crash_classifies_crashed_not_oom_killed(self, tmp_path, monkeypatch):
        _install_fake_worker(
            tmp_path, monkeypatch, "non_memory_crash_worker", _NON_MEMORY_CRASH_WORKER_SRC
        )
        cfg = _mask_config(tmp_path, n_cols=1)
        sources = _mask_sources(tmp_path, n_rows=5, n_cols=1)

        result = run_pipeline_isolated(cfg, sources, engine_version=_ENGINE_VERSION)

        assert result.outcome == "crashed"
        assert result.outputs is None


# --------------------------------------------------------------------------
# LOW-2: payload.json may carry source/target DSNs -- must be 0600.
# --------------------------------------------------------------------------


class TestPayloadFilePermissions:
    def test_payload_json_is_written_0600(self, tmp_path):
        staging_dir = tmp_path / "work_root"
        cfg = _mask_config(tmp_path, n_cols=1)
        sources = _mask_sources(tmp_path, n_rows=5, n_cols=1)

        run_pipeline_isolated(cfg, sources, engine_version=_ENGINE_VERSION, staging_dir=staging_dir)

        payload_path = staging_dir / "payload.json"
        assert payload_path.exists()
        mode = stat.S_IMODE(payload_path.stat().st_mode)
        assert mode == 0o600


# --------------------------------------------------------------------------
# LOW-3: signal.Signals(n) raises ValueError for a real-time signal number
# with no individual enum member -- must not crash the classifier.
# --------------------------------------------------------------------------


class TestSignalNameGuard:
    def test_real_time_signal_number_falls_back_to_numeric_label(self):
        from decoy_engine.execution._isolated_run import _signal_name

        rt_signal = int(signal.SIGRTMIN) + 5
        assert _signal_name(rt_signal) == f"signal {rt_signal}"

    def test_known_signal_number_resolves_its_name(self):
        from decoy_engine.execution._isolated_run import _signal_name

        assert _signal_name(int(signal.SIGKILL)) == "SIGKILL"

    def test_none_or_zero_returns_none(self):
        from decoy_engine.execution._isolated_run import _signal_name

        assert _signal_name(None) is None
        assert _signal_name(0) is None


# --------------------------------------------------------------------------
# Re-entry guard: an isolated child's `run_pipeline` can re-enter the routing
# layer (probe/governor), which calls `run_pipeline_isolated` again. With no
# "already isolated" guard, each level spawns another child -- a self-
# multiplying subprocess chain that saturated the host and had to be killed by
# hand (engine-efficiency streaming qualification, 2026-08-28). The marker set
# on every spawned child lets `run_pipeline_isolated` detect re-entry and run
# in-process instead of spawning a grandchild.
# --------------------------------------------------------------------------

_REENTRY_ENV = "DECOY_INSIDE_ISOLATED_WORKER"


class TestIsolationReentryGuard:
    def test_reentry_marker_forces_in_process_instead_of_spawning(self, tmp_path, monkeypatch):
        monkeypatch.setenv(_REENTRY_ENV, "1")

        def _no_spawn(*args, **kwargs):
            raise AssertionError(
                "run_pipeline_isolated spawned a subprocess from inside an "
                "isolated worker -- the self-multiplying grandchild runaway"
            )

        monkeypatch.setattr("decoy_engine.execution._isolated_run.subprocess.Popen", _no_spawn)

        cfg = _mask_config(tmp_path, n_cols=1)
        sources = _mask_sources(tmp_path, n_rows=10, n_cols=1)
        result = run_pipeline_isolated(cfg, sources, engine_version=_ENGINE_VERSION, isolate=True)

        assert result.outcome == "completed"
        assert result.isolated is False
        assert result.outputs is not None
        assert result.outputs["customers"].num_rows == 10

    def test_spawned_child_env_carries_the_reentry_marker(self, tmp_path, monkeypatch):
        import decoy_engine.execution._isolated_run as iso

        captured: dict[str, dict[str, str] | None] = {}
        real_popen = iso.subprocess.Popen

        def _spy_popen(cmd, *args, env=None, **kwargs):
            captured["env"] = env
            return real_popen(cmd, *args, env=env, **kwargs)

        monkeypatch.setattr(iso.subprocess, "Popen", _spy_popen)

        cfg = _mask_config(tmp_path, n_cols=1)
        sources = _mask_sources(tmp_path, n_rows=10, n_cols=1)
        result = run_pipeline_isolated(cfg, sources, engine_version=_ENGINE_VERSION, isolate=True)

        assert result.isolated is True
        assert captured["env"] is not None
        assert captured["env"].get(_REENTRY_ENV) == "1"

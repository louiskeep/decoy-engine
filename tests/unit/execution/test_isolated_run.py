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

import os
import signal
import threading

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.execution import IsolatedRunResult, run_pipeline_isolated

_ENGINE_VERSION = "isolated-run-test"


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
        cfg = _mask_config(tmp_path, n_cols=8)
        sources = _mask_sources(tmp_path, n_rows=200_000, n_cols=8)

        result = run_pipeline_isolated(
            cfg,
            sources,
            engine_version=_ENGINE_VERSION,
            mem_cap_bytes=64 * 1024 * 1024,
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

"""F29 acceptance A8: a failure reads the same on the direct, in-process and worker paths.

The direct call raises; `isolate=False` and the worker return an `IsolatedRunResult`. All three must
agree on identity, phase, retry advice and outcome. The parent validates whatever the worker sends,
an old or corrupt envelope degrades to a fixed diagnostic, and only the engine's own structured
route decisions (never arbitrary exception text) may change the governor's control flow.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path
from typing import Any

import pytest

from decoy_engine import reasons
from decoy_engine.errors import ConfigError
from decoy_engine.execution import _governor as governor
from decoy_engine.execution import run_pipeline
from decoy_engine.execution._isolated_common import IsolatedRunResult, exception_error_text
from decoy_engine.execution._isolated_run import _result_from_envelope, run_pipeline_isolated
from decoy_engine.keyprovider import MissingMaskSecret
from decoy_engine.reason_adapter import reason_from_exception
from decoy_engine.reasons import (
    CleanupState,
    Operation,
    Phase,
    PublicationState,
    ReasonRecord,
    RetryClass,
)

from ._f29_jobs import ENGINE_VERSION, fk_job

SECRET_REF = "env:F29_ISOLATION_NO_SUCH_SECRET"


def _shape(r: ReasonRecord) -> tuple:
    return (r.namespace, r.code, r.phase, r.retry_class, r.outcome, r.severity)


def test_direct_worker_parent_reason_parity(tmp_path: Path) -> None:
    cfg, sources = fk_job(tmp_path, mask_secret_ref=SECRET_REF)
    with pytest.raises(MissingMaskSecret) as info:
        run_pipeline(cfg, sources, engine_version=ENGINE_VERSION)
    direct = reason_from_exception(info.value)

    in_process = run_pipeline_isolated(cfg, sources, engine_version=ENGINE_VERSION, isolate=False)
    worker = run_pipeline_isolated(cfg, sources, engine_version=ENGINE_VERSION, isolate=True)

    # Coarse outcomes are unchanged by F29.
    assert in_process.outcome == worker.outcome == "crashed"
    assert in_process.reason is not None and worker.reason is not None
    assert _shape(in_process.reason) == _shape(direct) == _shape(worker.reason)
    assert direct.code == "missing_mask_secret" and direct.phase is Phase.RESOLVE
    assert direct.retry_class is RetryClass.CONFIGURATION_REPAIR
    # The coarse safe-summary text is exactly what it was before F29: the reason is additional.
    assert worker.error == in_process.error == "MissingMaskSecret"
    assert SECRET_REF not in repr(worker.reason) and SECRET_REF not in repr(in_process.reason)


def test_a_completed_run_has_no_reason(tmp_path: Path) -> None:
    cfg, sources = fk_job(tmp_path)
    result = run_pipeline_isolated(cfg, sources, engine_version=ENGINE_VERSION, isolate=False)
    assert result.outcome == "completed" and result.reason is None


@pytest.mark.parametrize("isolate", [False, True])
def test_a_failed_commit_is_a_publication_reason_on_both_commit_branches(
    tmp_path: Path, isolate: bool
) -> None:
    cfg, sources = fk_job(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "occupied.txt").write_text("x")  # a non-empty target makes the atomic rename fail
    result = run_pipeline_isolated(
        cfg, sources, engine_version=ENGINE_VERSION, isolate=isolate, output_dir=out
    )
    assert result.outcome == "crashed"
    assert result.reason is not None
    assert result.reason.code == "publication_failed"
    ctx = result.reason.context
    assert ctx.operation is Operation.COMMIT and ctx.storage_ref == "output"
    assert ctx.publication_state is PublicationState.NOT_PUBLISHED
    assert ctx.cleanup_state is CleanupState.CONFIRMED
    assert str(tmp_path) not in repr(result.reason)
    assert (out / "occupied.txt").read_text() == "x"  # nothing was published over it


def test_timeout_is_its_own_reason(tmp_path: Path) -> None:
    cfg, sources = fk_job(tmp_path)
    result = run_pipeline_isolated(
        cfg, sources, engine_version=ENGINE_VERSION, isolate=True, timeout_s=0.001
    )
    assert result.outcome == "crashed"
    assert result.reason is not None and result.reason.code == "isolated_run_timed_out"


def test_a_worker_that_dies_without_reporting_is_a_fixed_reason(tmp_path: Path) -> None:
    cfg, sources = fk_job(tmp_path)
    result = run_pipeline_isolated(
        cfg,
        sources,
        engine_version=ENGINE_VERSION,
        isolate=True,
        on_spawn=lambda pid: os.kill(pid, signal.SIGKILL),
    )
    assert result.outcome in ("oom_killed", "crashed")
    assert result.reason is not None
    expected = (
        "isolated_worker_out_of_memory"
        if result.outcome == "oom_killed"
        else "isolated_worker_died"
    )
    assert result.reason.code == expected


def _envelope(**kw: Any) -> dict[str, Any]:
    return {"outcome": "crashed", "peak_rss_mb": 1.0, "error": "ExecutionError", **kw}


def _from_envelope(envelope: dict[str, Any], tmp_path: Path) -> IsolatedRunResult:
    return _result_from_envelope(envelope, None, 1234, tmp_path / "staging")


def test_old_missing_corrupt_and_future_envelopes_degrade_safely(tmp_path: Path) -> None:
    good = reasons.ReasonRecord.for_reason(reasons.runtime.MISSING_MASK_SECRET, phase=Phase.RESOLVE)
    ok = _from_envelope(_envelope(reason=good.to_payload()), tmp_path)
    assert ok.reason == good

    old = _from_envelope(_envelope(), tmp_path)  # a worker from before F29: no reason key
    assert old.reason is not None and old.reason.code == "unexpected_error"
    old_oom = _from_envelope(_envelope(outcome="oom_killed"), tmp_path)
    assert old_oom.reason is not None and old_oom.reason.code == "isolated_worker_out_of_memory"

    for bad in ("not a mapping", 7, None, ["x"], {"schema_version": 1}):
        degraded = _from_envelope(_envelope(reason=bad), tmp_path).reason
        assert degraded is not None and degraded.code in {
            "reason_record_invalid",
            "unsupported_schema_version",
        }

    future = dict(good.to_payload(), schema_version=2)
    assert (
        _from_envelope(_envelope(reason=future), tmp_path).reason.code
        == "unsupported_schema_version"
    )
    newer = dict(good.to_payload(), code="a_code_from_a_newer_registry")
    parsed = _from_envelope(_envelope(reason=newer), tmp_path).reason
    assert parsed is not None and parsed.code == "unknown_reason_from_future"
    assert parsed.context.original_identity == ("runtime", "a_code_from_a_newer_registry")

    forged = dict(good.to_payload(), retry_class="retry_operation", outcome="success")
    cleaned = _from_envelope(_envelope(reason=forged), tmp_path).reason
    assert cleaned is not None
    assert cleaned.retry_class is RetryClass.CONFIGURATION_REPAIR  # recomputed, not believed


_MARKER = "is not sequential-eligible"


def test_arbitrary_exception_text_cannot_steer_the_governor() -> None:
    hostile = ValueError(f"a parser said the job {_MARKER}")
    assert _MARKER not in exception_error_text(hostile)
    engine = ConfigError(f"execution_mode='sequential' but the job {_MARKER}")
    assert _MARKER not in exception_error_text(engine)  # no exception text is a routing input

    class Raising(ConfigError):  # noqa: N818
        def __str__(self) -> str:
            raise RuntimeError("boom")

    # A hostile `__str__` never costs the primary failure.
    assert exception_error_text(Raising("x")).startswith("Raising")


def test_the_governor_does_not_reroute_on_marker_text_from_a_foreign_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_rung(config, sources, route, threshold, poll, kwargs):
        calls.append(route)
        return (
            IsolatedRunResult(
                outcome="crashed",
                peak_rss_mb=None,
                outputs=None,
                quality_metrics={},
                table_kinds={},
                returncode=0,
                signal_number=None,
                error=exception_error_text(ValueError(f"data said {_MARKER}")),
                isolated=True,
            ),
            None,
        )

    monkeypatch.setattr(governor, "_run_one_rung", fake_rung)
    outcome = governor.run_job_with_governor({}, None, budget_bytes=10**9, engine_version="x")
    assert outcome.outcome == "exhausted"
    assert calls == ["sequential"] or len(calls) == 1  # one rung: no reroute on that text
    assert [t.trip_kind for t in outcome.trips] == ["crashed"]


def test_successive_runs_do_not_share_failure_state(tmp_path: Path) -> None:
    cfg, sources = fk_job(tmp_path, mask_secret_ref=SECRET_REF)
    with pytest.raises(MissingMaskSecret) as first:
        run_pipeline(cfg, sources, engine_version=ENGINE_VERSION)
    ok_cfg, ok_sources = fk_job(tmp_path)
    result = run_pipeline(ok_cfg, ok_sources, engine_version=ENGINE_VERSION)
    assert result.outputs
    with pytest.raises(MissingMaskSecret) as second:
        run_pipeline(cfg, sources, engine_version=ENGINE_VERSION)
    assert reason_from_exception(first.value) == reason_from_exception(second.value)
    assert first.value is not second.value

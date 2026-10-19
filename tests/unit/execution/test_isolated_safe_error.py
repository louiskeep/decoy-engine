"""The user-visible `IsolatedRunResult.error` carries a safe summary, never raw text (OBS-1b).

Plan: docs/plans/2026-10-19-obs-1b-engine-log-leak-scrub.md section 2C. Every producer of the
field is exercised with a sentinel that stands in for a data value or a path: child stderr,
child stdout, the worker envelope, the in-process path, and both CommitError branches. Each
must keep the diagnostics (return code, signal, a stable reason, memory evidence) and the
route-ineligibility markers the governor matches, and must drop the sentinel.
"""

from __future__ import annotations

import signal
from typing import Any

import pytest

from decoy_engine.errors import ConfigError
from decoy_engine.execution import _governor, _isolated_run, run_pipeline_isolated
from decoy_engine.execution._governor import _is_route_ineligible_error, run_job_with_governor
from decoy_engine.execution._isolated_common import (
    ROUTE_INELIGIBLE_MARKERS,
    IsolatedRunResult,
    abnormal_exit_text,
    commit_error_text,
    exception_error_text,
)
from tests.unit.execution.test_governor_orchestration_kills import (
    _BUDGET,
    _CFG,
    _SRC,
    _completed_result,
    _crashed_result,
    _no_child,
    _oom_killed_result,
)
from tests.unit.execution.test_isolated_run import (
    _install_fake_worker,
    _mask_config,
    _mask_sources,
)

_SENTINEL = "SENTINEL_VALUE_8c1f"
_ENGINE_VERSION = "safe-error-test"
_OOC_MARKER = "is not out-of-core-eligible"
_SEQ_MARKER = "is not sequential-eligible"


def _spawn(tmp_path, monkeypatch, source: str, **kw: Any) -> IsolatedRunResult:
    _install_fake_worker(tmp_path, monkeypatch, "safe_error_worker", source)
    return run_pipeline_isolated({}, None, engine_version=_ENGINE_VERSION, **kw)


def _stream_worker(stream: str, text: str, exit_code: int) -> str:
    return f"import sys\nsys.{stream}.write({text!r})\nsys.exit({exit_code})\n"


class TestChildStreams:
    """(a) stderr and (b) stdout of a child that dies without an envelope."""

    @pytest.mark.parametrize("stream", ["stderr", "stdout"])
    def test_positive_exit_drops_the_stream_text_and_keeps_the_diagnostics(
        self, tmp_path, monkeypatch, stream
    ):
        source = _stream_worker(stream, f"ValueError: {_SENTINEL}\n", 3)
        result = _spawn(tmp_path, monkeypatch, source)
        assert result.outcome == "crashed"
        assert result.returncode == 3
        assert result.error == (
            "child terminated abnormally (returncode=3, reason=no_result_envelope)"
        )
        assert _SENTINEL not in result.error

    def test_memory_shaped_stderr_is_still_classified_and_named(self, tmp_path, monkeypatch):
        # Classification reads the ORIGINAL stderr; only the exposed text is scrubbed.
        source = _stream_worker("stderr", f"MemoryError: {_SENTINEL}\n", 1)
        result = _spawn(tmp_path, monkeypatch, source)
        assert result.outcome == "oom_killed"
        assert result.error == (
            "child terminated abnormally (returncode=1, reason=memory_marker_in_stderr)"
        )

    def test_signal_termination_keeps_the_signal_and_drops_stderr(self, tmp_path, monkeypatch):
        source = (
            "import os, signal, sys\n"
            f"sys.stderr.write({_SENTINEL!r})\nsys.stderr.flush()\n"
            "os.kill(os.getpid(), signal.SIGSEGV)\n"
        )
        result = _spawn(tmp_path, monkeypatch, source)
        assert result.outcome == "crashed"
        assert result.signal_number == signal.SIGSEGV
        assert result.error == (
            "child terminated abnormally (returncode=-11, signal=SIGSEGV, "
            "reason=no_result_envelope)"
        )

    def test_sigkill_with_a_memory_cap_keeps_signal_reason_and_memory_evidence(
        self, tmp_path, monkeypatch
    ):
        import os

        source = f"import sys, time\nsys.stderr.write({_SENTINEL!r})\ntime.sleep(999)\n"

        def kill(pid: int) -> None:
            os.kill(pid, signal.SIGKILL)

        result = _spawn(
            tmp_path, monkeypatch, source, mem_cap_bytes=64 * 1024 * 1024, on_spawn=kill
        )
        assert result.outcome == "oom_killed"
        assert result.error is not None
        assert result.error.startswith(
            "child terminated abnormally (returncode=-9, signal=SIGKILL, "
            "reason=memory_shaped_signal)"
        )
        assert _SENTINEL not in result.error
        assert result.memory_evidence is not None

    @pytest.mark.parametrize("stream", ["stderr", "stdout"])
    def test_a_route_marker_in_the_child_output_survives_for_the_governor(
        self, tmp_path, monkeypatch, stream
    ):
        text = f"ConfigError: the job {_OOC_MARKER} ({_SENTINEL})\n"
        result = _spawn(tmp_path, monkeypatch, _stream_worker(stream, text, 1))
        assert result.error is not None
        assert _OOC_MARKER in result.error
        assert _SENTINEL not in result.error
        assert _is_route_ineligible_error("out_of_core", result.error)


_ENVELOPE_WORKER = """
import sys
from decoy_engine.errors import ConfigError
from decoy_engine.execution import _isolated_worker as w

def boom(*_a, **_k):
    raise {raised}

w._load_sources = lambda *_a, **_k: {{}}
w.run_pipeline = boom
raise SystemExit(w.main(sys.argv))
"""


class TestWorkerEnvelope:
    """(c) an exception raised inside the real worker, serialized into the envelope."""

    def test_a_plain_exception_keeps_its_type_and_drops_the_message(self, tmp_path, monkeypatch):
        source = _ENVELOPE_WORKER.format(raised=f"ValueError({_SENTINEL!r})")
        result = _spawn(tmp_path, monkeypatch, source)
        assert result.outcome == "crashed"
        assert result.returncode == 0
        assert result.error == "ValueError"

    def test_a_memory_exception_is_still_oom_killed_without_its_message(
        self, tmp_path, monkeypatch
    ):
        source = _ENVELOPE_WORKER.format(raised=f"MemoryError({_SENTINEL!r})")
        result = _spawn(tmp_path, monkeypatch, source)
        assert result.outcome == "oom_killed"
        assert result.error == "MemoryError"

    @pytest.mark.parametrize(
        ("route", "marker"), [("out_of_core", _OOC_MARKER), ("sequential", _SEQ_MARKER)]
    )
    def test_the_route_marker_survives_the_boundary_and_the_reason_text_does_not(
        self, tmp_path, monkeypatch, route, marker
    ):
        message = f"execution_mode={route!r} requested but the job {marker} ({_SENTINEL})."
        source = _ENVELOPE_WORKER.format(raised=f"ConfigError({message!r})")
        result = _spawn(tmp_path, monkeypatch, source)
        assert result.outcome == "crashed"
        assert result.error is not None
        assert marker in result.error
        assert _SENTINEL not in result.error
        assert _is_route_ineligible_error(route, result.error)


class TestInProcess:
    """(d) the isolate=False fallback shares the summary builder with the worker."""

    @pytest.mark.parametrize(
        ("raised", "outcome", "expected"),
        [
            (ValueError(_SENTINEL), "crashed", "ValueError"),
            (MemoryError(_SENTINEL), "oom_killed", "MemoryError"),
        ],
    )
    def test_failure_text_is_the_type_only(
        self, monkeypatch, raised: BaseException, outcome: str, expected: str
    ):
        def boom(*_a: Any, **_k: Any) -> Any:
            raise raised

        monkeypatch.setattr(_isolated_run, "run_pipeline", boom)
        result = _isolated_run._run_in_process({}, None, {}, None)
        assert result.outcome == outcome
        assert result.error == expected

    def test_a_route_marker_survives(self, monkeypatch):
        def boom(*_a: Any, **_k: Any) -> Any:
            raise ConfigError(f"the job {_SEQ_MARKER} ({_SENTINEL})")

        monkeypatch.setattr(_isolated_run, "run_pipeline", boom)
        result = _isolated_run._run_in_process({}, None, {}, None)
        assert result.error == f"ConfigError: {_SEQ_MARKER}"


class TestCommitErrorBranches:
    """(e) in-process commit and (f) isolated-parent commit, with a path-bearing sentinel."""

    @pytest.mark.parametrize("isolate", [False, True], ids=["in_process_293", "isolated_560"])
    def test_a_failed_commit_drops_the_output_path_and_os_text(self, tmp_path, isolate):
        cfg = _mask_config(tmp_path, n_cols=1)
        sources = _mask_sources(tmp_path, n_rows=5, n_cols=1)
        output_dir = tmp_path / f"{_SENTINEL}_output"
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
        assert result.isolated is isolate
        assert result.committed_output_dir is None
        assert result.outputs is None
        assert result.error == (
            "CommitError: staged output could not be committed (errno=ENOTEMPTY)"
        )
        assert _SENTINEL not in result.error
        assert str(tmp_path) not in result.error
        # Behavior unchanged: the target is untouched and no staging is left behind.
        assert (output_dir / "preexisting.txt").read_text() == "do not clobber me"
        assert not list(output_dir.parent.glob("_decoy_isolated_stage_*"))


class TestSummaryBuilders:
    def test_every_governor_marker_survives_both_builders(self):
        for route, markers in ROUTE_INELIGIBLE_MARKERS.items():
            for marker in markers:
                from_exc = exception_error_text(RuntimeError(f"x {marker} {_SENTINEL}"))
                from_exit = abnormal_exit_text(1, None, f"x {marker} {_SENTINEL}", "")
                for text in (from_exc, from_exit):
                    assert marker in text
                    assert _SENTINEL not in text
                    assert _is_route_ineligible_error(route, text), (route, marker)  # type: ignore[arg-type]

    def test_the_governor_reads_the_same_marker_table_the_builders_write(self):
        assert _governor._INELIGIBLE_ROUTE_MARKERS is ROUTE_INELIGIBLE_MARKERS

    def test_an_execution_error_keeps_its_stable_code_only(self):
        from decoy_engine.execution._errors import ExecutionError

        exc = ExecutionError(code="fk_full_frame_oom_risk_rejected", message=_SENTINEL)
        assert exception_error_text(exc) == "ExecutionError [fk_full_frame_oom_risk_rejected]"

    def test_a_non_identifier_code_is_not_echoed(self):
        from decoy_engine.execution._errors import ExecutionError

        exc = ExecutionError(code=f"bad code {_SENTINEL}")
        assert exception_error_text(exc) == "ExecutionError"

    def test_a_commit_error_without_an_os_cause_has_no_errno(self):
        from decoy_engine.execution._isolated_commit import CommitError

        assert commit_error_text(CommitError(_SENTINEL)) == (
            "CommitError: staged output could not be committed"
        )


class TestGovernorStillReroutes:
    """The `error` field is a control-flow input; the safe summary must not change a verdict."""

    @staticmethod
    def _summary(message: str) -> str:
        return exception_error_text(ConfigError(message))

    def test_an_eligible_fallback_crash_still_reroutes(self, monkeypatch):
        _no_child(monkeypatch)
        ooc = self._summary(
            f"execution_mode='out_of_core' requested but the job {_OOC_MARKER} ({_SENTINEL})."
        )

        def fake_run(config, sources, *, execution_mode, on_spawn=None, **kw):
            if on_spawn is not None:
                on_spawn(1)
            if execution_mode == "full_frame":
                return _oom_killed_result(peak_rss_mb=200.0)
            if execution_mode == "out_of_core":
                return _crashed_result(ooc)
            return _completed_result()

        monkeypatch.setattr(_governor, "run_pipeline_isolated", fake_run)
        result = run_job_with_governor(_CFG, _SRC, budget_bytes=_BUDGET, poll_interval_s=0.01)

        assert result.outcome == "completed"
        assert result.final_route == "sequential"
        trip = result.trips[1]
        assert trip.trip_kind == "route_ineligible"
        assert trip.reroute_to == "sequential"
        assert _OOC_MARKER in (trip.error or "")
        assert _SENTINEL not in (trip.error or "")

    def test_a_genuine_crash_still_classifies_and_does_not_reroute(self, monkeypatch):
        _no_child(monkeypatch)
        genuine = exception_error_text(ValueError(f"unrelated bug {_SENTINEL}"))

        def fake_run(config, sources, *, execution_mode, on_spawn=None, **kw):
            return _crashed_result(genuine)

        monkeypatch.setattr(_governor, "run_pipeline_isolated", fake_run)
        result = run_job_with_governor(_CFG, _SRC, budget_bytes=_BUDGET, poll_interval_s=0.01)

        assert result.outcome == "exhausted"
        assert len(result.trips) == 1
        assert result.trips[0].trip_kind == "crashed"
        assert result.trips[0].reroute_to is None
        assert not _is_route_ineligible_error("full_frame", genuine)
        assert not _is_route_ineligible_error("out_of_core", genuine)
        assert not _is_route_ineligible_error("sequential", genuine)

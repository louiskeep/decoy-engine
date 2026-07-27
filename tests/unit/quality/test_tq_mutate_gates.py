"""Durable tests for the two soundness gates in `scripts/tq_mutate.py` added by
the Codex batch-gate remediation (finding #16 line of work):

- `baseline_sanity_check`'s forced-fail probe must require EXACTLY rc 1 (a genuine
  test failure), rejecting rc 0 (nothing killed) and rc 2/3/4/5 (harness errors).
- `main(--run)` must ABORT (return 2) when `mutmut run` exits nonzero, because a
  failed run leaves any on-disk `.meta` STALE and grading it would be meaningless.

These are the exit branches Codex flagged as untested (regression risk). `scripts`
is on `pythonpath` (pyproject) so `tq_mutate` imports directly.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest
import tq_mutate
from tq_mutate import BaselineError, MutmutConfig, RunResult, baseline_sanity_check


def _result(returncode: int) -> RunResult:
    return RunResult(
        returncode=returncode,
        duration_s=0.01,
        stdout="",
        stderr="",
        timed_out=False,
    )


def _selection_side_effect(forced_rc: int):
    """`_run_selection(config, mutants_dir, timeout, key)` -- original tree
    (key="") passes rc 0; the forced-fail probe (key="fail") returns `forced_rc`.
    """

    def _side(config, mutants_dir, timeout, key):
        return _result(0 if key == "" else forced_rc)

    return _side


_CFG = MutmutConfig(
    source_paths=["src"],
    only_mutate=["src/decoy_engine/execution/_sequential.py"],
    test_selection=["-k", "sequential"],
    extra_cli_args=[],
)


class TestForcedFailRequiresRc1:
    def test_rc1_passes_and_returns_baseline_seconds(self) -> None:
        with mock.patch.object(tq_mutate, "_run_selection", side_effect=_selection_side_effect(1)):
            seconds = baseline_sanity_check(_CFG, Path("mutants"), probe_timeout=30.0)
        assert seconds == pytest.approx(0.01)

    @pytest.mark.parametrize("bad_rc", [0, 2, 3, 4, 5])
    def test_non_rc1_forced_fail_aborts(self, bad_rc: int) -> None:
        # rc 0 = tests never call the mutated code (nothing killable); rc 2/3/4/5 =
        # harness failure, not a test failure. Both must fail the gate, not pass it.
        with mock.patch.object(
            tq_mutate, "_run_selection", side_effect=_selection_side_effect(bad_rc)
        ):
            with pytest.raises(BaselineError) as exc:
                baseline_sanity_check(_CFG, Path("mutants"), probe_timeout=30.0)
        assert "forced-fail probe" in str(exc.value)
        assert f"exited {bad_rc}" in str(exc.value)


class TestRunAbortsOnFailedMutmutRun:
    def _run_main(self, monkeypatch: pytest.MonkeyPatch, mutmut_rc: int):
        monkeypatch.setattr("sys.argv", ["tq_mutate", "--run"])
        monkeypatch.setattr(tq_mutate, "load_mutmut_config", lambda _p: _CFG)
        return monkeypatch

    def test_nonzero_mutmut_run_returns_2_before_grading(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._run_main(monkeypatch, mutmut_rc=3)
        # If the abort works, grading is never reached -- meta_paths_for raising
        # here would prove the opposite, so make it a tripwire.
        monkeypatch.setattr(
            tq_mutate,
            "meta_paths_for",
            mock.Mock(side_effect=AssertionError("graded a stale mutant set")),
        )
        with mock.patch.object(tq_mutate.subprocess, "call", return_value=3) as call:
            rc = tq_mutate.main()
        assert rc == 2
        call.assert_called_once_with(["mutmut", "run"])
        tq_mutate.meta_paths_for.assert_not_called()

    def test_clean_mutmut_run_proceeds_past_the_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._run_main(monkeypatch, mutmut_rc=0)
        # rc 0 must NOT abort: prove we reach grading by tripping the next step.
        sentinel = RuntimeError("reached grading")
        monkeypatch.setattr(tq_mutate, "meta_paths_for", mock.Mock(side_effect=sentinel))
        with mock.patch.object(tq_mutate.subprocess, "call", return_value=0):
            with pytest.raises(RuntimeError, match="reached grading"):
                tq_mutate.main()

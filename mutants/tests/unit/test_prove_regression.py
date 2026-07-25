"""Unit tests for the prove-regression gate logic (scripts/prove_regression.py)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "prove_regression.py"
_spec = importlib.util.spec_from_file_location("prove_regression", _SCRIPT)
assert _spec and _spec.loader
pr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pr)


def test_verdict_no_regression_test_fails() -> None:
    ok, msg = pr.verdict(regression_tests_found=False, baseline_pytest_rc=1)
    assert ok is False
    assert "No @pytest.mark.regression" in msg


def test_verdict_passing_on_baseline_fails() -> None:
    # rc == 0 means it passed on the pre-fix code: it does not prove the bug.
    ok, msg = pr.verdict(regression_tests_found=True, baseline_pytest_rc=0)
    assert ok is False
    assert "PASSES on the pre-fix baseline" in msg


def test_verdict_failing_on_baseline_passes() -> None:
    ok, _msg = pr.verdict(regression_tests_found=True, baseline_pytest_rc=1)
    assert ok is True


def test_verdict_inconclusive_exit_code_fails() -> None:
    # rc 2 = collection/usage error, not a clean assertion failure.
    ok, msg = pr.verdict(regression_tests_found=True, baseline_pytest_rc=2)
    assert ok is False
    assert "inconclusive" in msg


def test_verdict_errors_not_failures_is_inconclusive() -> None:
    # rc 1 but the run reported errors (import/fixture), not asserted failures.
    ok, msg = pr.verdict(
        regression_tests_found=True, baseline_pytest_rc=1, baseline_had_errors=True
    )
    assert ok is False
    assert "inconclusive" in msg


def test_regression_test_files_picks_marked_tests() -> None:
    files = {
        "tests/unit/test_bug.py": "import pytest\n@pytest.mark.regression\ndef test_x(): ...",
        "tests/unit/test_plain.py": "def test_y(): ...",
        "src/decoy_engine/thing.py": "@pytest.mark.regression maybe in a string",
        "tests/unit/test_modmark.py": "pytestmark = pytest.mark.regression",
    }
    found = pr.regression_test_files(sorted(files), lambda f: files[f])
    assert found == ["tests/unit/test_bug.py", "tests/unit/test_modmark.py"]


def test_regression_test_files_ignores_non_tests() -> None:
    # A marker string in non-test source must not count.
    files = {"src/decoy_engine/x.py": "@pytest.mark.regression"}
    assert pr.regression_test_files(list(files), lambda f: files[f]) == []

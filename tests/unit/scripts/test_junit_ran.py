"""AT-13: `junit_totals` counts JUnit files once, `junit_ran.py` says whether a suite ran clean.

CI jobs that must prove a suite really executed (dp-certified-gate, determinism) call the script;
the gate reporter prints the same counts through `junit_totals`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts.dev import _gates_report, junit_ran


def suite(tests: int, failures: int = 0, errors: int = 0, skipped: int = 0) -> str:
    return (
        f'<testsuite name="s" tests="{tests}" failures="{failures}" errors="{errors}" '
        f'skipped="{skipped}"></testsuite>'
    )


def write(tmp_path: Path, body: str, name: str = "r.xml") -> Path:
    path = tmp_path / name
    path.write_text(f'<?xml version="1.0"?>{body}')
    return path


def test_totals_read_a_bare_suite_and_sum_a_wrapper(tmp_path: Path) -> None:
    bare = write(tmp_path, suite(5, failures=1, errors=1, skipped=1), "a.xml")
    wrapped = write(tmp_path, f"<testsuites>{suite(3)}{suite(4, skipped=2)}</testsuites>", "b.xml")
    t = _gates_report.junit_totals([bare, wrapped])
    assert (t.tests, t.failures, t.errors, t.skipped, t.passed) == (12, 1, 1, 3, 7)


def test_gate_reporter_line_is_unchanged_by_the_extraction(tmp_path: Path) -> None:
    write(tmp_path, suite(10, failures=2, errors=1, skipped=3), "branch-tests.xml")
    write(tmp_path, f"<testsuites>{suite(4)}</testsuites>", "branch-tests-serial.xml")
    assert _gates_report._counts(tmp_path, "branch", "tests") == (
        "passed=8 failed=2 errors=1 skipped=3"
    )


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (suite(4), 0),
        (f"<testsuites>{suite(2)}{suite(2)}</testsuites>", 0),
        (suite(4, skipped=1), 1),
        (suite(0), 1),
        (suite(4, errors=1), 1),
        (suite(4, failures=1), 1),
        (f"<testsuites>{suite(2)}{suite(2, skipped=1)}</testsuites>", 1),
        ("<testsuite", 1),
    ],
)
def test_verdict(tmp_path: Path, body: str, code: int) -> None:
    assert junit_ran.main([str(write(tmp_path, body))]) == code


def test_missing_file_fails(tmp_path: Path) -> None:
    assert junit_ran.main([str(tmp_path / "nope.xml")]) == 1


def test_no_argument_is_a_usage_error() -> None:
    assert junit_ran.main([]) == 2


def test_output_names_counts_and_no_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    junit_ran.main([str(write(tmp_path, suite(4, skipped=1), "secret-name.xml"))])
    out = capsys.readouterr()
    assert "passed=3" in out.out + out.err and "secret-name" not in out.out + out.err

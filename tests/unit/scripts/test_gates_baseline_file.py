"""The stored failing-id baseline of `scripts/dev/gates.sh` (`--write-baseline`, `--baseline-file`).

A merge test runs the suite once and diffs its failing ids against a baseline written from a
clean origin/main. The baseline is only comparable while it is current and was written in the
same run mode, and a suite that quietly shrinks must not read as green."""

from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _gates_harness import (
    BASH,
    DEV,
    GIT,
    at_main,
    git,
    pytest_calls,
    recording_venv,
    report_module,
    seeded_clone,
)
from _gates_harness import gates as run_gates

pytestmark = pytest.mark.skipif(BASH is None or GIT is None, reason="needs bash and git")

WHOLE = ("--steps", "tests", "--full")
# Spelled out here, not taken from the reporter, so a wrong platform string in it shows up.
THIS_PLATFORM = f"{sys.platform}-{platform.machine()}"


def _write(clone: Path, venv: Path, dest: Path, **env: str) -> tuple[int, str]:
    return run_gates(clone, venv, *WHOLE, "--write-baseline", str(dest), **env)


def _stored(tmp_path: Path, main_sha: str, **extra: object) -> Path:
    mode = {"workers": 1, "dist": "no", "platform": THIS_PLATFORM, "order": "collection"}
    dest = tmp_path / "stored.json"
    dest.write_text(
        json.dumps(
            {
                "format_version": 1,
                "repo": "decoy-engine",
                "main_sha": main_sha,
                "head_sha": main_sha,
                "collected": 5,
                "skipped": 0,
                "failing": ["tests/a.py::test_one", "tests/a.py::test_three"],
                **mode,
                "order_seconds": {},
                "file_seconds": {},
            }
            | extra
        )
    )
    return dest


def _compare(clone: Path, venv: Path, stored: Path, *args: str, **env: str) -> tuple[int, str]:
    return run_gates(clone, venv, *(args or WHOLE), "--baseline-file", str(stored), **env)


def _origin_main(clone: Path) -> str:
    return git(clone, "rev-parse", "origin/main").strip()


# --- AT-9: the platform stored-baseline cases, on the engine ---------------------------------


def test_full_flag_runs_the_whole_tests_directory(tmp_path: Path) -> None:
    clone = seeded_clone(tmp_path)
    venv, log = recording_venv(tmp_path)
    _rc, out = run_gates(clone, venv, "--steps", "ruff,tests", "--full")
    assert "full suite (tests)" in out and "SELECTION ONLY" not in out, out
    calls = pytest_calls(log)
    assert len(calls) == 1 and calls[0][-1] == "tests", calls


def test_default_selection_is_labelled_not_the_full_suite(tmp_path: Path) -> None:
    clone = seeded_clone(tmp_path)
    venv, _log = recording_venv(tmp_path)
    _rc, out = run_gates(clone, venv, "--steps", "tests")
    assert "SELECTION ONLY (changed vs origin/main, 1 file(s) + always-on pins," in out


@pytest.mark.parametrize(
    "args",
    [
        ["--steps", "tests", "tests/_seeded"],
        ["--steps", "tests"],
        ["--steps", "ruff", "--full"],
    ],
    ids=["explicit-paths", "changed-selection", "no-tests-step"],
)
def test_write_baseline_refuses_a_selection(tmp_path: Path, args: list[str]) -> None:
    clone = seeded_clone(tmp_path)
    venv, log = recording_venv(tmp_path)
    dest = tmp_path / "base.json"
    rc, out = run_gates(clone, venv, "--write-baseline", str(dest), *args)
    assert rc == 2 and "selection, not the whole suite" in out, out
    assert not dest.exists() and pytest_calls(log) == []


@pytest.mark.parametrize(
    ("setup", "message"),
    [("seeded", "HEAD must equal origin/main"), ("dirty", "the tree must be clean")],
)
def test_write_baseline_refuses_a_head_that_is_not_a_clean_origin_main(
    tmp_path: Path, setup: str, message: str
) -> None:
    clone = seeded_clone(tmp_path)  # HEAD is one commit ahead of origin/main
    if setup == "dirty":
        at_main(clone)
        (clone / "stray.txt").write_text("x")
    venv, log = recording_venv(tmp_path)
    dest = tmp_path / "base.json"
    rc, out = _write(clone, venv, dest)
    assert rc == 2 and message in out, out
    assert not dest.exists() and pytest_calls(log) == []


def test_write_baseline_stores_the_failing_ids_and_the_run_mode(tmp_path: Path) -> None:
    clone = seeded_clone(tmp_path)
    main_sha = at_main(clone)
    venv, _log = recording_venv(tmp_path)
    dest = tmp_path / "base.json"
    rc, out = _write(
        clone, venv, dest, FAKE_FAIL="tests/b.py::test_two tests/a.py::test_one", FAKE_TOTAL="7"
    )
    assert rc == 1, out  # known failures still fail the plain run
    assert json.loads(dest.read_text()) == {
        "format_version": 1,
        "repo": "decoy-engine",
        "main_sha": main_sha,
        "head_sha": main_sha,
        "collected": 7,
        "skipped": 0,
        "failing": ["tests/a.py::test_one", "tests/b.py::test_two"],
        "workers": 1,
        "dist": "no",
        "platform": THIS_PLATFORM,
        "order": "collection",
        "order_seconds": {},
        "file_seconds": {"tests/a.py": 2.25, "tests/b.py": 1.0},
    }, out


def test_write_baseline_is_refused_when_an_earlier_step_stopped_the_run(tmp_path: Path) -> None:
    clone = seeded_clone(tmp_path)
    at_main(clone)
    venv, _log = recording_venv(tmp_path)
    (venv / "bin" / "ruff").write_text("#!/bin/sh\nexit 1\n")
    dest = tmp_path / "base.json"
    rc, out = run_gates(
        clone, venv, "--steps", "ruff,tests", "--full", "--write-baseline", str(dest)
    )
    assert rc != 0 and "did not finish the whole suite" in out, out
    assert not dest.exists()


def test_write_baseline_refuses_a_zero_test_junit(tmp_path: Path) -> None:
    clone = seeded_clone(tmp_path)
    at_main(clone)
    venv, _log = recording_venv(tmp_path)
    dest = tmp_path / "base.json"
    rc, out = _write(clone, venv, dest, FAKE_TOTAL="0")
    assert rc != 0 and "collected > 0" in out, out
    assert not dest.exists()


@pytest.mark.parametrize("env", [{"PYTEST_ADDOPTS": "-k chosen"}, {"PYTEST_PLUGINS": "deselect"}])
def test_write_baseline_ignores_a_selection_smuggled_in_through_the_environment(
    tmp_path: Path, env: dict[str, str]
) -> None:
    clone = seeded_clone(tmp_path)
    at_main(clone)
    venv, log = recording_venv(tmp_path)
    dest = tmp_path / "base.json"
    rc, out = _write(clone, venv, dest, **env)
    assert rc == 0, out
    assert json.loads(dest.read_text())["collected"] == 5, out
    assert "-o addopts=" in log.read_text()


def test_write_baseline_refuses_when_pytest_totals_disagree_with_the_junit(tmp_path: Path) -> None:
    clone = seeded_clone(tmp_path)
    at_main(clone)
    venv, _log = recording_venv(tmp_path)
    dest = tmp_path / "base.json"
    rc, out = _write(
        clone,
        venv,
        dest,
        FAKE_FAIL="tests/a.py::test_one",
        FAKE_TOTALS="2 failed, 3 passed in 0.01s",
    )
    assert rc != 0 and "pytest printed 2 failed/error but the JUnit has 1" in out, out
    assert "refusing to store the baseline" in out and not dest.exists()


@pytest.mark.parametrize(
    ("env", "ids"),
    [
        ({"FAKE_FAIL": "tests/a.py::test_p[a - b]"}, ["tests/a.py::test_p[a - b]"]),
        (
            {"FAKE_FAIL": "tests/a.py::test_p[case] - main]|tests/a.py::test_p[case] - branch]"},
            ["tests/a.py::test_p[case] - branch]", "tests/a.py::test_p[case] - main]"],
        ),
        ({"FAKE_FAIL": "tests/a.py::test_one", "FAKE_NOISE": "1"}, ["tests/a.py::test_one"]),
        ({"FAKE_FAIL": "tests/a.py::test_one", "FAKE_ANSI": "1"}, ["tests/a.py::test_one"]),
        ({"FAKE_FAIL": "tests/a.py::test_one", "FAKE_TRAILER": "1"}, ["tests/a.py::test_one"]),
    ],
    ids=["dash-in-param", "ids-that-collide-in-the-summary", "noise-lines", "ansi", "trailer"],
)
def test_write_baseline_takes_ids_from_the_junit_not_from_log_text(
    tmp_path: Path, env: dict[str, str], ids: list[str]
) -> None:
    clone = seeded_clone(tmp_path)
    at_main(clone)
    venv, _log = recording_venv(tmp_path)
    dest = tmp_path / "base.json"
    _write(clone, venv, dest, **env)
    assert json.loads(dest.read_text())["failing"] == ids


def test_write_baseline_stores_the_skipped_count_and_turns_colour_off(tmp_path: Path) -> None:
    clone = seeded_clone(tmp_path)
    at_main(clone)
    venv, log = recording_venv(tmp_path)
    dest = tmp_path / "base.json"
    _write(clone, venv, dest, FAKE_SKIPPED="3", PY_COLORS="1")
    assert json.loads(dest.read_text())["skipped"] == 3
    assert "--color=no" in log.read_text()


def test_baseline_file_reports_new_and_fixed_ids_and_blocks_on_a_new_one(tmp_path: Path) -> None:
    clone = seeded_clone(tmp_path)
    venv, log = recording_venv(tmp_path)
    rc, out = _compare(
        clone,
        venv,
        _stored(tmp_path, _origin_main(clone)),
        FAKE_FAIL="tests/a.py::test_one tests/a.py::test_two",
        FAKE_TOTAL="7",
    )
    assert rc == 1, out
    assert (
        "NEW-FAILING   tests/a.py::test_two" in out
        and "NEW-FAILING   tests/a.py::test_one" not in out
    )
    assert "NOW-PASSING   tests/a.py::test_three" in out, out
    assert "collected: baseline=5 branch=7 delta=+2" in out, out
    assert len(pytest_calls(log)) == 1, "origin/main must not be rerun"
    assert len(git(clone, "worktree", "list").splitlines()) == 1


def test_baseline_file_passes_when_only_known_ids_fail(tmp_path: Path) -> None:
    clone = seeded_clone(tmp_path)
    venv, _log = recording_venv(tmp_path)
    rc, out = _compare(
        clone, venv, _stored(tmp_path, _origin_main(clone)), FAKE_FAIL="tests/a.py::test_one"
    )
    assert rc == 0, out
    assert "NEW-FAILING" not in out and "WARNING:" not in out, out
    assert "(new=0 now-passing=1 known-still-failing=1)" in out, out


@pytest.mark.parametrize(
    ("args", "env"),
    [
        (list(WHOLE), {"FAKE_TOTAL": "0"}),
        (list(WHOLE), {"FAKE_NOJUNIT": "1"}),
        (["--steps", "ruff", "--full"], {}),
        (["--steps", "tests", "tests/_seeded"], {}),
    ],
    ids=["zero-test-junit", "missing-junit", "no-tests-step", "partial-selection"],
)
def test_baseline_file_fails_without_a_completed_whole_suite_run(
    tmp_path: Path, args: list[str], env: dict[str, str]
) -> None:
    clone = seeded_clone(tmp_path)
    venv, _log = recording_venv(tmp_path)
    rc, out = _compare(clone, venv, _stored(tmp_path, _origin_main(clone)), *args, **env)
    assert rc != 0, out
    assert "needs a completed whole-suite tests step" in out or "no step ran" in out, out


def test_baseline_file_fails_when_the_branch_skips_more_than_the_baseline(tmp_path: Path) -> None:
    clone = seeded_clone(tmp_path)
    venv, _log = recording_venv(tmp_path)
    rc, out = _compare(
        clone, venv, _stored(tmp_path, _origin_main(clone)),
        FAKE_FAIL="tests/a.py::test_one", FAKE_SKIPPED="2",
    )  # fmt: skip
    assert rc == 1 and "WARNING: branch skipped 2 tests, the baseline skipped 0" in out, out


def test_baseline_file_refuses_when_pytest_prints_no_totals_line(tmp_path: Path) -> None:
    clone = seeded_clone(tmp_path)
    venv, _log = recording_venv(tmp_path)
    rc, out = _compare(
        clone, venv, _stored(tmp_path, _origin_main(clone)),
        FAKE_FAIL="tests/a.py::test_one", FAKE_NOTOTALS="1",
    )  # fmt: skip
    assert rc == 3, out
    assert "pytest printed no totals line but the JUnit has 1" in out, out
    assert "refusing to compare against the baseline" in out and "disagrees with the JUnit" in out


def test_baseline_file_tells_apart_param_ids_that_differ_after_a_dash(tmp_path: Path) -> None:
    clone = seeded_clone(tmp_path)
    venv, _log = recording_venv(tmp_path)
    stored = _stored(tmp_path, _origin_main(clone), failing=["tests/a.py::test_p[case] - main]"])
    rc, out = _compare(clone, venv, stored, FAKE_FAIL="tests/a.py::test_p[case] - branch]")
    assert rc == 1 and "NEW-FAILING   tests/a.py::test_p[case] - branch]" in out, out


def test_junit_ids_from_a_real_pytest_run_keep_params_classes_and_errors(tmp_path: Path) -> None:
    src = tmp_path / "proj" / "tests" / "sub"
    src.mkdir(parents=True)
    (src / "test_x.py").write_text(
        "import pytest\n"
        '@pytest.mark.parametrize("v", ["case] - main", "case] - branch", "x::y"])\n'
        "def test_p(v): assert False\n"
        "class TestA:\n"
        "    def test_m(self): assert False\n"
        "    class TestInner:\n"
        "        def test_n(self): assert False\n"
        "@pytest.fixture\n"
        "def boom(): raise RuntimeError\n"
        "def test_setup_err(boom): pass\n"
    )
    (src.parent / "test_coll.py").write_text("import nothing_here_at_all\n")
    xml = tmp_path / "o.xml"
    subprocess.run(  # noqa: S603 -- fixed argv, tiny tmp project
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "junit_family=xunit1",
         "--continue-on-collection-errors", f"--junitxml={xml}", "tests"],
        cwd=tmp_path / "proj", capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    out = tmp_path / "out"
    out.mkdir()
    shutil.copy(xml, out / "branch-tests.xml")
    f = "tests/sub/test_x.py"
    assert sorted(report_module().junit_failing_ids(out, "branch")) == sorted(
        [
            f"{f}::test_p[case] - main]",
            f"{f}::test_p[case] - branch]",
            f"{f}::test_p[x::y]",
            f"{f}::TestA::test_m",
            f"{f}::TestA::TestInner::test_n",
            f"{f}::test_setup_err",
            "tests/test_coll.py",
        ]
    )


def test_totals_count_matches_the_junit_on_a_real_run_with_teardown_errors(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    (proj / "tests").mkdir(parents=True)
    (proj / "tests" / "test_t.py").write_text(
        "import pytest\n"
        "@pytest.fixture\n"
        "def td():\n"
        "    yield\n"
        "    raise RuntimeError\n"
        "def test_fail_and_teardown(td): assert False\n"
        "def test_pass_but_teardown(td): pass\n"
    )
    out = tmp_path / "out"
    out.mkdir()
    run = subprocess.run(  # noqa: S603 -- fixed argv, tiny tmp project
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "junit_family=xunit1",
         f"--junitxml={out / 'branch-tests.xml'}", "tests"],
        cwd=proj, capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    (out / "branch-tests.log").write_text(run.stdout)
    mod = report_module()
    assert mod.pytest_totals_failed(out, "branch") == 3, run.stdout
    assert mod.junit_failure_elements(out, "branch") == 3


def test_help_prints_the_whole_header_comment() -> None:
    proc = subprocess.run(  # noqa: S603 -- fixed argv
        [str(BASH), "scripts/dev/gates.sh", "--help"],
        cwd=DEV.parents[1], capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.rstrip().endswith("Use --full (or `tests`)."), proc.stdout


# --- AT-8: a baseline is only compared with a run of the same mode ---------------------------


def test_a_baseline_written_by_gates_is_comparable_by_gates(tmp_path: Path) -> None:
    clone = seeded_clone(tmp_path)
    at_main(clone)
    venv, _log = recording_venv(tmp_path)
    dest = tmp_path / "base.json"
    _write(clone, venv, dest, FAKE_FAIL="tests/a.py::test_one")
    rc, out = _compare(clone, venv, dest, FAKE_FAIL="tests/a.py::test_one")
    assert rc == 0, out
    assert "workers=1" in out, out


_OTHER_PLATFORM = "darwin-arm64" if not THIS_PLATFORM.startswith("darwin") else "linux-x86_64"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("workers", 4),
        ("dist", "loadfile"),
        ("platform", _OTHER_PLATFORM),
        ("order", "from-a-map"),
    ],
)
def test_a_baseline_from_another_run_mode_is_refused_before_running(
    tmp_path: Path, field: str, value: object
) -> None:
    clone = seeded_clone(tmp_path)
    venv, log = recording_venv(tmp_path)
    extra: dict[str, object] = {field: value}
    if field == "order":  # a label that is not the hash of the map it came with
        extra = {"order_seconds": {"tests/a.py": 3.0}, "order": "file-seconds:deadbeef"}
        value = extra["order"]
    stored = _stored(tmp_path, _origin_main(clone), **extra)
    rc, out = _compare(clone, venv, stored)
    assert rc == 2, out
    assert f"run mode differs in {field}" in out and "rewrite the baseline in this mode" in out, out
    assert str(value) in out and "baseline {" in out and "this run {" in out, out
    assert pytest_calls(log) == []


def test_a_baseline_ordered_by_a_map_is_compared_in_that_order(tmp_path: Path) -> None:
    """A candidate orders by the baseline's own order_seconds, so its mode matches (AT-8)."""
    clone = seeded_clone(tmp_path)
    venv, _log = recording_venv(tmp_path)
    seconds = {"tests/a.py": 3.0}
    stored = _stored(
        tmp_path, _origin_main(clone), order_seconds=seconds,
        order=report_module().order_label(seconds),
    )  # fmt: skip
    rc, out = _compare(clone, venv, stored, FAKE_FAIL="tests/a.py::test_one")
    assert rc == 0, out


@pytest.mark.parametrize(
    ("drop", "message"),
    [
        ("workers", "no workers"),
        ("order_seconds", "no order_seconds"),
        ("file_seconds", "no file_seconds"),
        ("skipped", "no skipped"),
        ("collected", "no collected"),
        ("failing", "no failing"),
        ("main_sha", "no main_sha"),
        ("format_version", "has format_version None"),
    ],
)
def test_a_baseline_without_the_run_mode_fields_is_refused(
    tmp_path: Path, drop: str, message: str
) -> None:
    clone = seeded_clone(tmp_path)
    venv, _log = recording_venv(tmp_path)
    stored = _stored(tmp_path, _origin_main(clone))
    data = json.loads(stored.read_text())
    del data[drop]
    stored.write_text(json.dumps(data))
    rc, out = _compare(clone, venv, stored)
    assert rc == 2 and message in out, out


def test_a_baseline_for_another_repo_is_refused(tmp_path: Path) -> None:
    clone = seeded_clone(tmp_path)
    venv, _log = recording_venv(tmp_path)
    rc, out = _compare(clone, venv, _stored(tmp_path, _origin_main(clone), repo="decoy-platform"))
    assert rc == 2 and "is for repo 'decoy-platform'" in out, out


# --- AT-16: a stored baseline is only compared while it is current ---------------------------


def _main_moves(clone: Path, files: dict[str, str]) -> str:
    """Move origin/main (and HEAD) forward by one commit; return the old main."""
    old = _origin_main(clone)
    for rel, body in files.items():
        (clone / rel).parent.mkdir(parents=True, exist_ok=True)
        (clone / rel).write_text(body)
    git(clone, "add", "-A")
    git(clone, "commit", "-qm", "main moves")
    at_main(clone)
    return old


def _stale_run(tmp_path: Path, files: dict[str, str], before: dict[str, str] | None = None):
    clone = seeded_clone(tmp_path)
    at_main(clone)  # the stored baseline is of the seeded head, so only the moves below differ
    if before:  # files that exist on both sides, so a test can name them
        _main_moves(clone, before)
    venv, log = recording_venv(tmp_path)
    old = _main_moves(clone, files)
    rc, out = _compare(clone, venv, _stored(tmp_path, old), FAKE_FAIL="tests/a.py::test_one")
    return rc, out, log


def test_a_baseline_behind_a_code_move_of_main_is_refused(tmp_path: Path) -> None:
    rc, out, log = _stale_run(tmp_path, {"src/decoy_engine/_par_probe.py": "X = 1\n"})
    assert rc == 2 and "is stale against origin/main" in out and "rewrite it" in out, out
    assert pytest_calls(log) == []


def test_a_baseline_behind_an_unread_markdown_move_of_main_is_used(tmp_path: Path) -> None:
    # Built in two parts: this very file sits under tests/, so a literal name would be "named".
    rc, out, _log = _stale_run(tmp_path, {"docs/par-" + "unread-probe.md": "# x\n"})
    assert rc == 0, out
    assert "main moved only in Markdown no test reads" in out, out


def test_a_baseline_behind_a_markdown_a_test_names_is_refused(tmp_path: Path) -> None:
    named = {"tests/_seeded/test_names_doc.py": "DOC = 'docs/par-named.md'\n"}
    rc, out, _log = _stale_run(tmp_path, {"docs/par-named.md": "# x\n"}, before=named)
    assert rc == 2 and "which a test names" in out, out


def test_a_baseline_behind_a_markdown_only_a_test_helper_names_is_refused(tmp_path: Path) -> None:
    helper = {"tests/_seeded/_names_doc.py": "DOC = 'docs/par-helper.md'\n"}
    rc, out, _log = _stale_run(tmp_path, {"docs/par-helper.md": "# x\n"}, before=helper)
    assert rc == 2 and "which a test names" in out, out


@pytest.mark.parametrize(
    "path", ["docs/conf.py", "docs/methodology-registry.yaml", "tests/_seeded/test_new.py"]
)
def test_a_baseline_behind_a_non_markdown_move_is_refused(tmp_path: Path, path: str) -> None:
    rc, out, _log = _stale_run(tmp_path, {path: "x = 1\n"})
    assert rc == 2 and "is stale against origin/main" in out, out


def test_a_baseline_behind_a_markdown_a_sentry_walks_is_refused(tmp_path: Path) -> None:
    walked = "docs/plans/h1-wave-b/manifests/par-probe.md"
    rc, out, _log = _stale_run(tmp_path, {walked: "# x\n"})
    assert rc == 2 and "which a sentry walks" in out, out


def test_a_baseline_from_a_commit_this_repository_lacks_is_refused(tmp_path: Path) -> None:
    clone = seeded_clone(tmp_path)
    venv, _log = recording_venv(tmp_path)
    rc, out = _compare(clone, venv, _stored(tmp_path, "deadbeef"))
    assert rc == 2 and "is not in this repository" in out, out


# --- AT-17: a whole-suite run never shrinks silently -----------------------------------------


def _shrink_clone(tmp_path: Path) -> Path:
    """origin/main holds test_gone.py; the branch commit is made by the caller."""
    clone = seeded_clone(tmp_path)
    (clone / "tests" / "_seeded" / "test_gone.py").write_text("def test_g():\n    pass\n")
    git(clone, "add", "-A")
    git(clone, "commit", "-qm", "gone exists")
    at_main(clone)
    return clone


def test_a_run_that_collects_ten_fewer_ids_than_the_baseline_fails(tmp_path: Path) -> None:
    clone = _shrink_clone(tmp_path)
    venv, _log = recording_venv(tmp_path)
    stored = _stored(tmp_path, _origin_main(clone), collected=15)
    rc, out = _compare(clone, venv, stored, FAKE_FAIL="tests/a.py::test_one", FAKE_TOTAL="5")
    assert rc == 1, out
    assert "collects 10 fewer tests than the baseline but deleted only 0" in out, out


def test_a_branch_that_deleted_the_test_file_holding_those_ids_passes(tmp_path: Path) -> None:
    clone = _shrink_clone(tmp_path)
    git(clone, "rm", "-q", "tests/_seeded/test_gone.py")
    git(clone, "commit", "-qm", "delete gone")
    venv, _log = recording_venv(tmp_path)
    stored = _stored(tmp_path, _origin_main(clone), collected=15)
    env = {"FAKE_FAIL": "tests/a.py::test_one", "FAKE_TOTAL": "5", "FAKE_COLLECT": "10"}
    rc, out = _compare(clone, venv, stored, **env)
    assert rc == 0, out
    assert "collected: baseline=15 branch=5 delta=-10" in out, out
    assert "collected 10 fewer tests than the baseline; the branch deleted 10" in out, out
    assert len(git(clone, "worktree", "list").splitlines()) == 1
    rc, out = _compare(clone, venv, stored, **env | {"FAKE_COLLECT": "9"})
    assert rc == 1 and "deleted only 9" in out, out


def test_a_renamed_test_file_is_not_a_deletion(tmp_path: Path) -> None:
    clone = _shrink_clone(tmp_path)
    git(clone, "mv", "tests/_seeded/test_gone.py", "tests/_seeded/test_moved.py")
    git(clone, "commit", "-qm", "rename gone")
    venv, _log = recording_venv(tmp_path)
    stored = _stored(tmp_path, _origin_main(clone), collected=15)
    env = {"FAKE_FAIL": "tests/a.py::test_one", "FAKE_TOTAL": "5", "FAKE_COLLECT": "10"}
    rc, out = _compare(clone, venv, stored, **env)
    assert rc == 1 and "deleted only 0" in out, out


def test_a_bigger_suite_than_the_baseline_is_not_a_shrink(tmp_path: Path) -> None:
    clone = seeded_clone(tmp_path)
    venv, _log = recording_venv(tmp_path)
    rc, out = _compare(
        clone, venv, _stored(tmp_path, _origin_main(clone)),
        FAKE_FAIL="tests/a.py::test_one", FAKE_TOTAL="9",
    )  # fmt: skip
    assert rc == 0, out


def test_the_reporter_names_the_file_it_ordered_seconds_by(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "branch-tests.xml").write_text(
        '<testsuite tests="3" failures="0" errors="0" skipped="0">'
        '<testcase classname="t.a" name="x" file="t/a.py" time="1.5"/>'
        '<testcase classname="t.a" name="y" file="t/a.py" time="0.5"/>'
        '<testcase classname="t.b" name="z" file="t/b.py" time="0.25"/>'
        "</testsuite>"
    )
    assert report_module().file_seconds(out, "branch") == {"t/a.py": 2.0, "t/b.py": 0.25}


# --- review fixes: strict stored-file types, docs-walking sentries, one owner, inherited ids --


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("collected", True),
        ("collected", "5"),
        ("collected", -1),
        ("skipped", False),
        ("skipped", 0.5),
        ("failing", "tests/a.py::test_one"),
        ("failing", [1, 2]),
        ("failing", None),
        ("main_sha", ""),
        ("main_sha", 7),
        ("file_seconds", []),
        ("format_version", 2),
        ("format_version", True),
        ("format_version", "1"),
        ("workers", True),
        ("workers", 0),
        ("workers", 1.0),
        ("dist", 1),
        ("platform", None),
        ("order", 3),
        ("repo", None),
        ("head_sha", 5),
        ("order_seconds", {"tests/a.py": True}),
        ("order_seconds", {"tests/a.py": -1}),
        ("file_seconds", {"tests/a.py": "1"}),
    ],
)
def test_a_baseline_with_a_wrongly_typed_or_unknown_field_is_refused(
    tmp_path: Path, field: str, value: object
) -> None:
    clone = seeded_clone(tmp_path)
    venv, log = recording_venv(tmp_path)
    stored = _stored(tmp_path, _origin_main(clone))
    data = json.loads(stored.read_text()) | {field: value}
    stored.write_text(json.dumps(data))
    rc, out = _compare(clone, venv, stored, FAKE_FAIL="tests/a.py::test_one")
    assert rc == 2 and "rewrite the baseline" in out, out
    assert pytest_calls(log) == []


def test_a_baseline_with_an_unknown_field_is_refused(tmp_path: Path) -> None:
    clone = seeded_clone(tmp_path)
    venv, _log = recording_venv(tmp_path)
    rc, out = _compare(clone, venv, _stored(tmp_path, _origin_main(clone), extra_field=1))
    assert rc == 2 and "unknown field(s) extra_field" in out, out


def test_a_corrupt_baseline_cannot_hide_lost_tests_or_a_skip_flood(tmp_path: Path) -> None:
    # Codex's mutation: JSON true read as workers=1 and false as collected=0, with a huge
    # skipped count, let a smaller suite that skips more exit 0.
    clone = seeded_clone(tmp_path)
    venv, _log = recording_venv(tmp_path)
    stored = _stored(tmp_path, _origin_main(clone), workers=True, collected=False, skipped=999999)
    rc, out = _compare(
        clone, venv, stored, FAKE_FAIL="tests/a.py::test_one", FAKE_TOTAL="3", FAKE_SKIPPED="2"
    )
    assert rc == 2 and "wrong type" in out, out


@pytest.mark.parametrize(
    ("order", "order_seconds"),
    [("file-seconds:deadbeef", {}), ("collection", {"tests/a.py": 1.0})],
)
def test_a_baseline_whose_order_label_does_not_match_its_map_is_refused(
    tmp_path: Path, order: str, order_seconds: dict[str, float]
) -> None:
    clone = seeded_clone(tmp_path)
    venv, _log = recording_venv(tmp_path)
    stored = _stored(tmp_path, _origin_main(clone), order=order, order_seconds=order_seconds)
    rc, out = _compare(clone, venv, stored, FAKE_FAIL="tests/a.py::test_one")
    assert rc == 2 and "run mode differs in order" in out, out


@pytest.mark.parametrize(
    "walker",
    [
        "tests/sentry/test_sdv_import_sentry.py::test_no_citation_labels_sdv_permissive",
        "tests/sentry/test_perf_claim_provenance.py::test_no_unsourced_perf_claims_in_docs",
        "tests/sentry/test_some_future_docs_walker.py::test_x",
        "tests/secret_providers/test_leak_sentry.py::test_leak",
    ],
)
def test_a_baseline_holding_a_docs_walking_sentry_failure_is_stale_after_any_move(
    tmp_path: Path, walker: str
) -> None:
    # A Markdown-only commit on main could fix this id and the branch re-break it, so the
    # docs-only exception does not apply while the id is in the stored failing list.
    clone = seeded_clone(tmp_path)
    at_main(clone)
    venv, log = recording_venv(tmp_path)
    old = _main_moves(clone, {"docs/par-" + "unread-probe.md": "# x\n"})
    stored = _stored(tmp_path, old, failing=["tests/a.py::test_one", walker])
    rc, out = _compare(clone, venv, stored, FAKE_FAIL="tests/a.py::test_one")
    assert rc == 2 and "sentry failure that a Markdown change on main could have hidden" in out, out
    assert pytest_calls(log) == []


_ALLOWED_PY_ARGS = (
    ("-m", "pytest"),
    ("--version",),
    ("]",),  # `[ -x "$BIN/python" ]` existence test
    ("$ROOT/scripts/dev/_gates_report.py",),
    ('${junit[@]+"${junit[@]}"}',),  # args for the `bash -c` wrapper, built by gates.sh itself
)


def gates_sh_violations(text: str) -> list[str]:
    """Reasons gates.sh could read the stored file or JUnit outside _gates_report.py.

    By construction: every python call must run the reporter, `-m pytest` or `--version`.
    Any inline program (-c), stdin program (-), heredoc python, aliasing of the interpreter
    or jq is refused, so a renamed json import cannot slip past a substring list.
    """
    bad: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            continue
        if re.search(r"(^|[^\w])jq(\s|$)", line):
            bad.append(f"jq: {line}")
        if "python" not in line:
            continue
        if "<<" in line:
            bad.append(f"heredoc python: {line}")
        if re.search(r"^(export\s+|local\s+)?\w+=[\"']?[^\s\"'()]*python", line):
            bad.append(f"python aliased: {line}")
        for m in re.finditer(r'(?<![\w-])python[\d.]*(?![\w:])"?([^\n]*)', line):
            args = tuple(t.strip(')"') for t in m.group(1).split()[:2])
            if args and not any(args[: len(a)] == a for a in _ALLOWED_PY_ARGS):
                bad.append(f"python runs something else: {line}")
    return bad


def test_gates_sh_runs_python_only_through_allow_listed_programs() -> None:
    # One owner per concern: the stored file and the JUnit are read only by _gates_report.py.
    assert gates_sh_violations((DEV / "gates.sh").read_text()) == []


@pytest.mark.parametrize(
    "line",
    [
        '"$BIN/python" -c \'from json import load; print(load(open("baseline.json")))\'',
        '"$BIN/python" -c \'import json as j; j.load(open("b"))\'',
        '"$BIN/python" -c \'__import__("json").load(open("b"))\'',
        '"$BIN/python" - <<EOF',
        '"$BIN/python" - < prog.py',
        "PY=$BIN/python",
        'jq .main_sha "$BASEFILE"',
        "x=$(echo '{}' | jq -r .a)",
    ],
)
def test_the_gates_sh_guard_refuses_each_seeded_reader(line: str) -> None:
    assert gates_sh_violations("set -e\n" + line + "\n"), line


def test_reporter_subcommands_replace_the_inline_parsing(tmp_path: Path) -> None:
    mod = DEV / "_gates_report.py"
    out = tmp_path / "out"
    out.mkdir()
    (out / "branch-tests.xml").write_text("<testsuite")  # truncated
    zero = subprocess.run(  # noqa: S603 -- fixed argv
        [sys.executable, str(mod), "--suite-collected", str(out)],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert zero.stdout.strip() == "0"
    (out / "branch-tests.xml").write_text(
        '<testsuite tests="3" failures="0" errors="0" skipped="0"/>'
    )
    three = subprocess.run(  # noqa: S603 -- fixed argv
        [sys.executable, str(mod), "--suite-collected", str(out)],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert three.stdout.strip() == "3"
    stored = _stored(tmp_path, "abc123")
    shown = subprocess.run(  # noqa: S603 -- fixed argv
        [sys.executable, str(mod), "--check-stored", str(stored), "decoy-engine", "1"],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert shown.returncode == 0 and shown.stdout.strip() == "abc123"


_INHERITED_JUNIT = (
    '<testsuite tests="2" failures="1" errors="0" skipped="0">'
    '<testcase classname="tests.test_child.TestChild" name="test_inherited" '
    'file="tests/_base.py" time="2.0"><failure/></testcase>'
    '<testcase classname="tests.test_child.TestChild" name="test_own" '
    'file="tests/test_child.py" time="1.0"/>'
    "</testsuite>"
)


def test_an_inherited_test_is_keyed_by_the_collected_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "_base.py").write_text("")
    (root / "tests" / "test_child.py").write_text("")
    out = tmp_path / "out"
    out.mkdir()
    (out / "branch-tests.xml").write_text(_INHERITED_JUNIT)
    mod = report_module()
    monkeypatch.setattr(mod, "TEST_ROOT", root)
    assert mod.junit_failing_ids(out, "branch") == [
        "tests/test_child.py::TestChild::test_inherited"
    ]
    assert mod.file_seconds(out, "branch") == {"tests/test_child.py": 3.0}


def test_an_inherited_test_from_a_real_run_has_a_rerunnable_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = tmp_path / "proj"
    (proj / "tests").mkdir(parents=True)
    (proj / "tests" / "__init__.py").write_text("")
    (proj / "tests" / "_base.py").write_text(
        "class Contract:\n    def test_inherited(self): assert False\n"
    )
    (proj / "tests" / "test_child.py").write_text(
        "from tests._base import Contract\nclass TestChild(Contract):\n    pass\n"
    )
    xml = tmp_path / "o.xml"
    subprocess.run(  # noqa: S603 -- fixed argv, tiny tmp project
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "junit_family=xunit1",
         f"--junitxml={xml}", "tests/test_child.py"],
        cwd=proj, capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    out = tmp_path / "out"
    out.mkdir()
    shutil.copy(xml, out / "branch-tests.xml")
    mod = report_module()
    monkeypatch.setattr(mod, "TEST_ROOT", proj)
    assert mod.junit_failing_ids(out, "branch") == [
        "tests/test_child.py::TestChild::test_inherited"
    ]
    assert set(mod.file_seconds(out, "branch")) == {"tests/test_child.py"}

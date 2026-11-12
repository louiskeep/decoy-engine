"""scripts/dev/gates.sh with GATES_WORKERS > 1: the two passes, the reporter, mode and order."""

from __future__ import annotations

import json
import os
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
    REPO,
    at_main,
    git,
    pytest_argvs,
    pytest_calls,
    recording_venv,
    report_module,
    run,
    seeded_clone,
)
from _gates_harness import gates as run_gates
from tests.sentry.parallel._resources import parse_marker_expr

pytestmark = pytest.mark.skipif(BASH is None or GIT is None, reason="needs bash and git")

TESTS_ONLY = ("--steps", "tests", "tests/_seeded")
WHOLE = ("--steps", "tests", "--full")
ORDER_ENV = "DECOY_TEST_ORDER_FILE"


def mark_expr() -> str:
    return parse_marker_expr((DEV / "gates.sh").read_text())


def commit(clone: Path, message: str) -> None:
    git(clone, "add", "-A")
    proc = run(
        [str(GIT), "-c", "user.name=gates-test", "-c", "user.email=gates-test@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "--no-verify", "--allow-empty", "-qm", message],
        clone,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stdout + proc.stderr


def parallel_clone(tmp_path: Path) -> Path:
    """A seeded clone that carries the xdist report scrubber, as a tree after this change does."""
    clone = seeded_clone(tmp_path)
    shutil.copy2(REPO / "tests" / "_xdist_safe_reports.py", clone / "tests")
    commit(clone, "scrubber")
    at_main(clone)
    return clone


def test_the_sentry_step_runs_in_strict_venv_mode(tmp_path: Path) -> None:
    """The registry's zero-unverified check only runs when the gate says the venv is complete."""
    clone = parallel_clone(tmp_path)
    venv, log = recording_venv(tmp_path)
    rc, out = run_gates(clone, venv, "--steps", "sentry", "--full")
    assert rc == 0, out
    assert log.with_name("strict.log").read_text().split() == ["1"]


# --- AT-1, AT-2, AT-4 ---------------------------------------------------------------------


def test_workers_one_is_the_plain_single_pytest_run(tmp_path: Path) -> None:
    clone = parallel_clone(tmp_path)
    venv, log = recording_venv(tmp_path)
    rc, out = run_gates(clone, venv, *TESTS_ONLY, GATES_WORKERS="1")
    assert rc == 0, out
    (call,) = pytest_calls(log)
    assert "-n" not in call and "--dist" not in call and "serial" not in " ".join(call), call
    assert mark_expr() in " ".join(call)
    assert "workers:    1 (GATES_WORKERS=1)" in out, out
    assert "tests-serial" not in out, out


def test_workers_two_run_a_parallel_pass_then_a_serial_pass(tmp_path: Path) -> None:
    clone = parallel_clone(tmp_path)
    venv, log = recording_venv(tmp_path)
    rc, out = run_gates(clone, venv, *TESTS_ONLY, GATES_WORKERS="2", FAKE_TOTAL="3")
    assert rc == 0, out
    assert "workers:    2 (GATES_WORKERS=2)" in out, out
    par, ser = pytest_calls(log)  # exactly two pytest runs, in this order
    mark = mark_expr()
    assert par[par.index("-n") + 1] == "2" and par[par.index("--dist") + 1] == "loadfile", par
    assert f"({mark}) and not serial" in " ".join(par), par
    assert "-n" not in ser and "--dist" not in ser, ser
    assert f"({mark}) and serial" in " ".join(ser), ser
    assert any(a.endswith("branch-tests.xml") for a in par), par
    assert any(a.endswith("branch-tests-serial.xml") for a in ser), ser
    assert re.search(r"\[tests\] PASS passed=4 failed=0", out), out  # 3 plain + 1 serial


def test_an_empty_serial_pass_is_not_a_failure(tmp_path: Path) -> None:
    clone = parallel_clone(tmp_path)
    venv, _log = recording_venv(tmp_path)
    rc, out = run_gates(clone, venv, *TESTS_ONLY, GATES_WORKERS="2", FAKE_NO_SERIAL="1")
    assert rc == 0, out
    assert re.search(r"\[tests\] PASS passed=5 failed=0", out), out


def test_the_serial_pass_runs_and_counts_when_the_parallel_pass_fails(tmp_path: Path) -> None:
    clone = parallel_clone(tmp_path)
    venv, log = recording_venv(tmp_path)
    rc, out = run_gates(
        clone, venv, *TESTS_ONLY, GATES_WORKERS="2", FAKE_FAIL="tests/a.py::test_x", FAKE_TOTAL="3"
    )
    assert rc == 1, out
    assert len(pytest_calls(log)) == 2, "the serial pass must run after a parallel failure"
    assert re.search(r"\[tests\] FAIL\(rc=1\)", out), out
    assert "tests/a.py::test_x" in out.split("FAILING TEST IDS", 1)[-1], out


def test_a_failure_only_in_the_serial_pass_is_a_failing_id(tmp_path: Path) -> None:
    clone = parallel_clone(tmp_path)
    venv, _log = recording_venv(tmp_path)
    rc, out = run_gates(
        clone, venv, *TESTS_ONLY, GATES_WORKERS="2", FAKE_SERIAL_FAIL="s_bad", FAKE_TOTAL="3"
    )
    assert rc == 1, out
    assert "tests/s.py::s_bad" in out.split("FAILING TEST IDS", 1)[-1], out


# --- AT-10: a tree without the scrubber stays serial ---------------------------------------


def test_a_baseline_tree_without_the_scrubber_runs_serial_and_says_so(tmp_path: Path) -> None:
    clone = seeded_clone(tmp_path)
    scrubber = clone / "tests" / "_xdist_safe_reports.py"
    scrubber.unlink(missing_ok=True)  # origin/main predates the scrubber ...
    commit(clone, "main without the scrubber")
    at_main(clone)
    shutil.copy2(REPO / "tests" / "_xdist_safe_reports.py", scrubber)  # ... the branch adds it
    commit(clone, "branch adds the scrubber")
    venv, log = recording_venv(tmp_path)
    rc, out = run_gates(clone, venv, *TESTS_ONLY, "--baseline", GATES_WORKERS="2")
    assert rc == 0, out
    calls = pytest_calls(log)
    par = [c for c in calls if "-n" in c]
    assert len(par) == 1, calls  # the branch side only
    base_section = out.split("-- baseline --", 1)[1]
    assert "no tests/_xdist_safe_reports.py" in base_section and " -n " not in base_section, out


# --- AT-3: the passes partition the real selection -----------------------------------------


def last_mark(call: list[str]) -> str:
    """The `-m` value that wins: the last one on the command line."""
    return call[len(call) - call[::-1].index("-m")]


def clean_env() -> dict[str, str]:
    """This process's environment without the outer pytest/xdist state a child run must not see."""
    return {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_") and k != ORDER_ENV}


def collected_ids(expr: str, *targets: str) -> set[str]:
    proc = subprocess.run(  # noqa: S603 -- literals plus this interpreter
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
         "-p", "no:xdist", "-o", "addopts=", "--strict-markers", "-m", expr, *targets],
        cwd=REPO, env=clean_env(), capture_output=True, text=True, timeout=900, check=False,
    )  # fmt: skip
    assert proc.returncode in (0, 5), proc.stdout[-1500:] + proc.stderr[-1500:]
    return {ln for ln in proc.stdout.splitlines() if "::" in ln}


def test_the_two_passes_partition_the_real_selection(tmp_path: Path) -> None:
    """Take the two -m expressions from what gates.sh runs, then collect the real tree."""
    clone = parallel_clone(tmp_path)
    venv, log = recording_venv(tmp_path)
    rc, out = run_gates(clone, venv, *TESTS_ONLY, GATES_WORKERS="2")
    assert rc == 0, out
    par, ser = pytest_argvs(log)
    par_expr, ser_expr = (last_mark(c) for c in (par, ser))
    scope = ("tests/perf", "tests/connectors/test_gcs_timeouts.py", "tests/unit/scripts")
    parallel = collected_ids(par_expr, *scope)
    serial = collected_ids(ser_expr, *scope)
    everything = collected_ids(mark_expr(), *scope)
    assert serial, "no test carries the serial marker"
    assert parallel, "nothing left for the parallel pass"
    assert not parallel & serial, "a test would run in both passes"
    assert parallel | serial == everything


# --- AT-5, AT-4 reporter --------------------------------------------------------------------

_CASES = (
    '<testcase classname="tests.test_a" name="test_ok" file="tests/test_a.py"/>'
    '<testcase classname="tests.test_a" name="test_bad" file="tests/test_a.py"><failure/></testcase>'
    '<testcase classname="tests.test_a" name="test_skip" file="tests/test_a.py"><skipped/></testcase>'
    '<testcase classname="tests.test_b.TestK" name="test_ok" file="tests/test_b.py"/>'
)
_SERIAL_CASES = (
    '<testcase classname="tests.test_s.TestS" name="test_timing" file="tests/test_s.py"><failure/></testcase>'
    '<testcase classname="tests.test_s.TestS" name="test_mem" file="tests/test_s.py"/>'
)


def _suite(cases: str, tests: int, failures: int, skipped: int) -> str:
    return (
        f'<testsuites><testsuite name="pytest" tests="{tests}" failures="{failures}" errors="0" '
        f'skipped="{skipped}">{cases}</testsuite></testsuites>'
    )


def _out_dir(root: Path, name: str, two_files: bool) -> Path:
    out = root / name
    out.mkdir()
    (out / "branch.steps").write_text("tests\t1\tpytest\n")
    if two_files:
        (out / "branch-tests.xml").write_text(_suite(_CASES, 4, 1, 1))
        (out / "branch-tests-serial.xml").write_text(_suite(_SERIAL_CASES, 2, 1, 0))
        (out / "branch-tests.log").write_text("1 failed, 1 passed, 1 skipped in 1.00s (0:00:01)\n")
        (out / "branch-tests-serial.log").write_text("1 failed, 1 passed in 0.50s\n")
    else:
        (out / "branch-tests.xml").write_text(_suite(_CASES + _SERIAL_CASES, 6, 2, 1))
        (out / "branch-tests.log").write_text("2 failed, 3 passed, 1 skipped in 1.50s\n")
    return out


def test_reporter_counts_are_identical_for_one_or_two_junit_files(tmp_path: Path) -> None:
    gr = report_module()
    one = _out_dir(tmp_path, "one", two_files=False)
    two = _out_dir(tmp_path, "two", two_files=True)
    assert gr.collected(one, "branch") == gr.collected(two, "branch") == 6
    assert gr.skipped(one, "branch") == gr.skipped(two, "branch") == 1
    assert gr._counts(one, "branch", "tests") == gr._counts(two, "branch", "tests")
    assert sorted(gr.junit_failing_ids(one, "branch")) == sorted(
        gr.junit_failing_ids(two, "branch")
    )
    assert gr.junit_failing_ids(two, "branch") == [
        "tests/test_a.py::test_bad",
        "tests/test_s.py::TestS::test_timing",
    ]
    assert gr.junit_failure_elements(one, "branch") == gr.junit_failure_elements(two, "branch") == 2
    assert gr.pytest_totals_failed(one, "branch") == gr.pytest_totals_failed(two, "branch") == 2
    assert gr.branch_only_steps(two) == gr.branch_only_steps(one) == ["tests"]


def test_reporter_refuses_a_serial_log_without_a_totals_line(tmp_path: Path) -> None:
    two = _out_dir(tmp_path, "two", two_files=True)
    (two / "branch-tests-serial.log").write_text("Killed\n")
    assert report_module().pytest_totals_failed(two, "branch") is None


def test_write_baseline_stores_ids_from_both_passes(tmp_path: Path) -> None:
    two = _out_dir(tmp_path, "two", two_files=True)
    dest = tmp_path / "base.json"
    report_module().write_baseline(two, dest, "decoy-engine", "m" * 40, "h" * 40, 4)
    data = json.loads(dest.read_text())
    assert data["collected"] == 6 and data["skipped"] == 1
    assert data["failing"] == ["tests/test_a.py::test_bad", "tests/test_s.py::TestS::test_timing"]
    assert data["workers"] == 4 and data["dist"] == "loadfile"


# --- AT-6: a crashed worker is a failing id --------------------------------------------------


def test_a_worker_crash_is_a_failing_id_of_that_test(tmp_path: Path) -> None:
    pytest.importorskip("xdist")
    (tmp_path / "test_crash.py").write_text(
        "import os\n\ndef test_ok():\n    pass\n\ndef test_dies():\n    os.abort()\n"
    )
    junit = tmp_path / "out.xml"
    proc = subprocess.run(  # noqa: S603 -- literals plus this interpreter
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "-n", "2",
         "-o", "junit_family=xunit1", f"--junitxml={junit}", "--rootdir", str(tmp_path),
         str(tmp_path / "test_crash.py")],
        cwd=tmp_path, capture_output=True, text=True, timeout=300, check=False,
    )  # fmt: skip
    assert proc.returncode == 1, proc.stdout + proc.stderr  # a crash is a failure, not a pass
    out = tmp_path / "o"
    out.mkdir()
    shutil.copy(junit, out / "branch-tests.xml")
    ids = report_module().junit_failing_ids(out, "branch")
    assert any(i.endswith("test_crash.py::test_dies") for i in ids), ids


# --- AT-8: mode-bound baselines round trip -----------------------------------------------------


def _write_parallel(clone: Path, venv: Path, dest: Path, *extra: str, **env: str):
    return run_gates(clone, venv, *WHOLE, "--write-baseline", str(dest), *extra, **env)


def test_a_parallel_baseline_records_workers_and_dist(tmp_path: Path) -> None:
    clone = parallel_clone(tmp_path)
    venv, _log = recording_venv(tmp_path)
    dest = tmp_path / "a.json"
    rc, out = _write_parallel(clone, venv, dest, GATES_WORKERS="2", FAKE_TOTAL="3")
    assert rc == 0, out
    data = json.loads(dest.read_text())
    assert (data["workers"], data["dist"], data["order"], data["order_seconds"]) == (
        2, "loadfile", "collection", {},
    )  # fmt: skip
    rc, out = run_gates(clone, venv, *WHOLE, "--baseline-file", str(dest), GATES_WORKERS="1")
    assert rc == 2 and "run mode differs in workers, dist" in out, out


def test_order_round_trip_write_a_then_b_then_compare(tmp_path: Path) -> None:
    clone = parallel_clone(tmp_path)
    venv, _log = recording_venv(tmp_path)
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    ord_log = tmp_path / "order.log"
    env = {"GATES_WORKERS": "2", "FAKE_TOTAL": "3", "FAKE_ORDER_LOG": str(ord_log)}
    rc, out = _write_parallel(clone, venv, a, **env)
    assert rc == 0, out
    seconds = json.loads(a.read_text())["file_seconds"]
    assert seconds, "the fake run must record per-file seconds"
    rc, out = _write_parallel(clone, venv, b, "--order-from", str(a), **env)
    assert rc == 0, out
    data = json.loads(b.read_text())
    gr = report_module()
    assert data["order_seconds"] == seconds and data["order"] == gr.order_label(seconds)
    assert data["order"].startswith("file-seconds:")
    ord_log.write_text("")
    rc, out = run_gates(clone, venv, *WHOLE, "--baseline-file", str(b), **env)
    assert rc == 0, out
    # The candidate hands the suite B.order_seconds, never B.file_seconds.
    seen = [json.loads(x) for x in ord_log.read_text().split("\n") if x]
    assert seen and all(x == {"order_seconds": seconds} for x in seen), seen


def test_a_compare_orders_by_order_seconds_not_file_seconds(tmp_path: Path) -> None:
    clone = parallel_clone(tmp_path)
    venv, _log = recording_venv(tmp_path)
    gr = report_module()
    order = {"tests/a.py": 9.0}
    stored = tmp_path / "s.json"
    mode = gr.run_mode(2, order)
    stored.write_text(json.dumps({
        "format_version": 1, "repo": "decoy-engine", "main_sha": git(clone, "rev-parse", "HEAD").strip(),
        "head_sha": "x", "collected": 4, "skipped": 0, "failing": [], **mode,
        "order_seconds": order, "file_seconds": {"tests/z.py": 99.0},
    }))  # fmt: skip
    ord_log = tmp_path / "order.log"
    rc, out = run_gates(
        clone, venv, *WHOLE, "--baseline-file", str(stored),
        GATES_WORKERS="2", FAKE_TOTAL="3", FAKE_ORDER_LOG=str(ord_log),
    )  # fmt: skip
    assert rc == 0, out
    assert json.loads(ord_log.read_text().split("\n")[0]) == {"order_seconds": order}


def test_order_from_is_refused_without_a_write(tmp_path: Path) -> None:
    clone = parallel_clone(tmp_path)
    venv, _log = recording_venv(tmp_path)
    rc, out = run_gates(clone, venv, *TESTS_ONLY, "--order-from", str(tmp_path / "x.json"))
    assert rc == 2 and "--order-from only applies to --write-baseline" in out, out


def test_a_p1_format_baseline_is_comparable_with_a_p2_candidate_at_one_worker(
    tmp_path: Path,
) -> None:
    clone = parallel_clone(tmp_path)
    venv, _log = recording_venv(tmp_path)
    main = git(clone, "rev-parse", "HEAD").strip()
    stored = tmp_path / "p1.json"
    stored.write_text(json.dumps({
        "format_version": 1, "repo": "decoy-engine", "main_sha": main, "head_sha": main,
        "collected": 5, "skipped": 0, "failing": [], "workers": 1, "dist": "no",
        "platform": report_module().run_mode(1, {})["platform"], "order": "collection",
        "order_seconds": {}, "file_seconds": {},
    }))  # fmt: skip
    rc, out = run_gates(clone, venv, *WHOLE, "--baseline-file", str(stored), GATES_WORKERS="1")
    assert rc == 0, out


# --- AT-15: collection order from the order map ----------------------------------------------

_SMALL = (
    "tests/unit/scripts/test_bench_sink_merge.py",
    "tests/unit/scripts/test_faker_determinism_digest_codec.py",
    "tests/unit/scripts/test_fk_scale_infra.py",
)


def _collect_order(order: dict[str, object] | None, tmp_path: Path) -> list[str]:
    env = clean_env()
    if order is not None:
        path = tmp_path / "order.json"
        path.write_text(json.dumps({"order_seconds": order}))
        env["DECOY_TEST_ORDER_FILE"] = str(path)
    proc = subprocess.run(  # noqa: S603 -- literals plus this interpreter
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
         "-p", "no:xdist", "-o", "addopts=", *_SMALL],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=600, check=False,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stdout[-1500:] + proc.stderr[-1500:]
    return [ln for ln in proc.stdout.splitlines() if "::" in ln]


def test_collection_puts_the_longest_files_first_and_keeps_each_files_order(
    tmp_path: Path,
) -> None:
    plain = _collect_order(None, tmp_path)
    ordered = _collect_order({_SMALL[2]: 30.0, _SMALL[0]: 5.0}, tmp_path)
    assert sorted(plain) == sorted(ordered), "the same ids either way"
    files = [ln.split("::")[0] for ln in ordered]
    firsts = list(dict.fromkeys(files))
    assert firsts == [_SMALL[2], _SMALL[0], _SMALL[1]], firsts  # unlisted file sorts last
    for f in _SMALL:  # each file's own order is untouched
        assert [x for x in ordered if x.startswith(f)] == [x for x in plain if x.startswith(f)]


@pytest.mark.parametrize("order", [None, {}])
def test_an_empty_or_absent_order_map_keeps_collection_order(
    tmp_path: Path, order: dict[str, object] | None
) -> None:
    assert _collect_order(order, tmp_path) == _collect_order(None, tmp_path)


# --- AT-18: directory marks come from the collection hook -------------------------------------


def test_every_perf_item_carries_serial_at_collection() -> None:
    expr = mark_expr()
    perf = collected_ids(f"({expr}) and serial", "tests/perf")
    everything = collected_ids(expr, "tests/perf")
    assert perf and perf == everything
    assert collected_ids(f"({expr}) and not serial", "tests/perf") == set()

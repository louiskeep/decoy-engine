"""Shared fixtures for the gates.sh tests: scratch clones, a recording venv and a fake pytest.

The fake `python -m pytest` writes the JUnit file and the totals line a real run would, driven
by FAKE_* environment variables, so the stored-baseline cases run in milliseconds without a suite."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DEV = REPO / "scripts" / "dev"
BASH = shutil.which("bash")
GIT = shutil.which("git")


def read_pins(pins: Path = DEV / "gate_pins.txt") -> list[str]:
    """The pinned test paths of gate_pins.txt: `#` comments and surrounding blanks dropped."""
    lines = (ln.split("#")[0].strip() for ln in pins.read_text().splitlines())
    return sorted(ln for ln in lines if ln)


def run(
    argv: list[str], cwd: Path, check: bool = False, **extra: str
) -> subprocess.CompletedProcess[str]:
    # Point the scripts at the interpreter running this test so nothing is installed.
    # One worker unless a test asks for more: `auto` would depend on the host running the tests.
    env = dict(os.environ, DECOY_VENV=sys.prefix, GATES_WORKERS="1") | extra
    env.pop("VIRTUAL_ENV", None)
    return subprocess.run(  # noqa: S603 -- argv is test literals plus resolved tool paths
        argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=600, check=check
    )


def git(cwd: Path, *args: str) -> str:
    ident = ["-c", "user.name=gates-test", "-c", "user.email=gates-test@example.invalid"]
    return run([str(GIT), *ident, *args], cwd, check=True).stdout


def _clone_with_current_scripts(tmp_path: Path) -> Path:
    clone = tmp_path / "repo"
    git(tmp_path, "clone", "-q", "--shared", str(REPO), str(clone))
    # Test the scripts in this working tree, not whatever the clone's HEAD carries.
    shutil.rmtree(clone / "scripts" / "dev", ignore_errors=True)
    shutil.copytree(DEV, clone / "scripts" / "dev")
    return clone


def seeded_clone(tmp_path: Path) -> Path:
    """A clone whose origin/main has one shared failure and whose HEAD adds one more."""
    clone = _clone_with_current_scripts(tmp_path)
    seed = clone / "tests" / "_seeded"
    seed.mkdir()
    (seed / "test_seed_shared.py").write_text("def test_shared():\n    assert False\n")
    (seed / "test_seed_ok.py").write_text("def test_ok():\n    assert True\n")
    git(clone, "add", "-A")
    git(clone, "commit", "-qm", "base")
    git(clone, "update-ref", "refs/remotes/origin/main", "HEAD")
    (seed / "test_seed_branch.py").write_text("def test_branch():\n    assert False\n")
    git(clone, "add", "-A")
    git(clone, "commit", "-qm", "seed")
    return clone


def diff_clone(tmp_path: Path, branch_files: dict[str, str]) -> Path:
    """A clone whose origin/main is HEAD~1 and whose HEAD adds `branch_files`."""
    clone = _clone_with_current_scripts(tmp_path)
    git(clone, "add", "-A")
    git(clone, "commit", "--allow-empty", "-qm", "base")
    git(clone, "update-ref", "refs/remotes/origin/main", "HEAD")
    for rel, body in branch_files.items():
        (clone / rel).parent.mkdir(parents=True, exist_ok=True)
        (clone / rel).write_text(body)
    git(clone, "add", "-A")
    git(clone, "commit", "-qm", "branch")
    return clone


def report_module():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("_gates_report", DEV / "_gates_report.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve string annotations through sys.modules
    spec.loader.exec_module(mod)
    return mod


FAKE_PYTEST = """\
import json, os, sys
from pathlib import Path

argv = sys.argv[1:]
with open(Path(__file__).with_name("argv.jsonl"), "a") as _fh:  # exact argv, spaces kept
    _fh.write(json.dumps(argv) + "\\n")
with open(Path(__file__).with_name("strict.log"), "a") as _fh:  # the sentry step's strictness switch
    _fh.write(os.environ.get("DECOY_GATE_VENV_STRICT", "") + "\\n")
if "--collect-only" in argv:
    for i in range(int(os.environ.get("FAKE_COLLECT", "0"))):
        print(f"tests/_seeded/test_gone.py::test_{i}")
    sys.exit(0)
xml = next(a.split("=", 1)[1] for a in argv if a.startswith("--junitxml="))
order_log = os.environ.get("FAKE_ORDER_LOG")
if order_log:  # what the suite would read as its collection order (parallel and serial pass alike)
    src = os.environ.get("DECOY_TEST_ORDER_FILE", "")
    with open(order_log, "a") as fh:
        fh.write((Path(src).read_text() if src else "unset") + "\\n")
mark = next((argv[i + 1] for i in reversed(range(len(argv) - 1)) if argv[i] == "-m"), "")
if mark.endswith(" and serial"):  # the serial pass of a parallel run
    if os.environ.get("FAKE_NO_SERIAL"):
        Path(xml).write_text('<testsuites><testsuite name="pytest" tests="0" failures="0" errors="0" skipped="0"/></testsuites>')
        print("no tests ran in 0.01s")
        sys.exit(5)
    sfail = os.environ.get("FAKE_SERIAL_FAIL", "")
    n_ok = int(os.environ.get("FAKE_SERIAL_TOTAL", "1"))
    cases = "".join(f'<testcase classname="tests.s" name="s{i}" file="tests/s.py" time="0.5"/>' for i in range(n_ok))
    if sfail:
        cases += f'<testcase classname="tests.s" name="{sfail}" file="tests/s.py" time="0.5"><failure/></testcase>'
    Path(xml).write_text(
        f'<testsuites><testsuite name="pytest" tests="{n_ok + bool(sfail)}" failures="{int(bool(sfail))}" errors="0" skipped="0">{cases}</testsuite></testsuites>'
    )
    print(f"{int(bool(sfail))} failed, {n_ok} passed in 0.01s" if sfail else f"{n_ok} passed in 0.01s")
    sys.exit(1 if sfail else 0)
raw = os.environ.get("FAKE_FAIL", "")
failing = raw.split("|") if "|" in raw or "[" in raw else raw.split()
total = int(os.environ.get("FAKE_TOTAL", "5"))
if os.environ.get("PYTEST_ADDOPTS") or os.environ.get("PYTEST_PLUGINS"):
    total = 1  # a selection from the environment (addopts or a plugin) shrinks the run
xunit1 = "junit_family=xunit1" in " ".join(argv)


def split_top(t):
    parts, depth, cur, i = [], 0, "", 0
    while i < len(t):
        if t[i] == "[":
            depth += 1
        elif t[i] == "]":
            depth -= 1
        if depth == 0 and t.startswith("::", i):
            parts.append(cur)
            cur, i = "", i + 2
            continue
        cur += t[i]
        i += 1
    return parts + [cur]


def failing_case(t):
    path, *rest = split_top(t)
    if not rest:
        return f'<testcase classname="" name="{path}" file="{path}"><error/></testcase>'
    module = path.removesuffix(".py").replace("/", ".")
    cls = "".join("." + c for c in rest[:-1])
    attr = f' file="{path}" time="1.0"' if xunit1 else ""
    name = rest[-1].replace("&", "&amp;").replace("<", "&lt;")
    return f'<testcase classname="{module}{cls}" name="{name}"{attr}><failure/></testcase>'


ok = (' file="tests/a.py" time="0.25"' if xunit1 else "")
cases = "".join(
    f'<testcase classname="tests.a" name="n{i}"{ok}/>' for i in range(total - len(failing))
) + "".join(failing_case(t) for t in failing)
if not os.environ.get("FAKE_NOJUNIT"):
    Path(xml).write_text(
        f'<testsuites><testsuite name="pytest" tests="{total}" failures="{len(failing)}" '
        f'errors="0" skipped="{os.environ.get("FAKE_SKIPPED", "0")}">{cases}</testsuite></testsuites>'
    )
for t in failing:
    print(f"FAILED {t} - AssertionError")
if os.environ.get("FAKE_NOISE"):
    print("ERROR    logger:x.py:1 msg")
    print("FAILED tests/fake.py::x")
totals = os.environ.get("FAKE_TOTALS", f"{len(failing)} failed, {total - len(failing)} passed in 0.01s")
if not os.environ.get("FAKE_NOTOTALS"):
    if os.environ.get("FAKE_TRAILER"):
        print(totals)
        totals = "atexit: closed pool in 0.3s"
    if os.environ.get("FAKE_ANSI") or (os.environ.get("PY_COLORS") == "1"):
        totals = "\\x1b[1m\\x1b[31m" + totals + "\\x1b[0m"
    print(totals)
sys.exit(1 if failing else 0)
"""


def recording_venv(tmp_path: Path) -> tuple[Path, Path]:
    """A venv whose pytest, ruff and mypy only record argv, so no real suite starts."""
    venv = tmp_path / "recording-venv"
    (venv / "bin").mkdir(parents=True)
    log = tmp_path / "argv.log"
    helper = tmp_path / "fake_pytest.py"
    helper.write_text(FAKE_PYTEST)
    python = venv / "bin" / "python"
    # Only `python -m pytest` is faked; the reporter and version probes run for real.
    python.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = -m ] && [ "$2" = pytest ] && [ "$3" != --version ]; then\n'
        f'  echo "pytest $*" >>"{log}"\n'
        f'  exec "{sys.executable}" "{helper}" "$@"\n'
        "fi\n"
        f'exec "{sys.executable}" "$@"\n'
    )
    python.chmod(0o755)
    for tool in ("ruff", "mypy"):
        exe = venv / "bin" / tool
        exe.write_text(f'#!/bin/sh\necho "{tool} $*" >>"{log}"\n')
        exe.chmod(0o755)
    return venv, log


def at_main(clone: Path) -> str:
    """Make origin/main equal HEAD, as a baseline write requires."""
    git(clone, "update-ref", "refs/remotes/origin/main", "HEAD")
    return git(clone, "rev-parse", "HEAD").strip()


def gates(clone: Path, venv: Path, *args: str, **env: str) -> tuple[int, str]:
    proc = run([str(BASH), "scripts/dev/gates.sh", *args], clone, DECOY_VENV=str(venv), **env)
    return proc.returncode, proc.stdout + proc.stderr


def pytest_calls(log: Path) -> list[list[str]]:
    lines = log.read_text().splitlines() if log.exists() else []
    return [ln.split()[1:] for ln in lines if ln.startswith("pytest ")]


def pytest_argvs(log: Path) -> list[list[str]]:
    """Each fake pytest call as its exact argv (a `-m` expression stays one element)."""
    path = log.with_suffix(".jsonl")
    lines = path.read_text().splitlines() if path.exists() else []
    return [json.loads(ln) for ln in lines]

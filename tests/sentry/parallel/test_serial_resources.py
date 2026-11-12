"""Parallel-safety sentry (ENGINE-PAR P2): a test that needs the whole host is marked `serial`.

`gates.sh` runs `serial` tests alone after the parallel pass. Two checks run on one real
collection of the suite (a scrubbed subprocess, cached per session):

1. an AST scan finds tests that assert a wall-clock bound, a memory envelope, a speedup ratio,
   an absolute thread count, a fixed port or an in-process rlimit; any such test without the
   mark must be on the shrink-only allowlist (`baselines/serial-resources.json`);
2. every test the required-serial registry names must carry the mark at collection.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _resources as res

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
REGISTRY = HERE / "required_serial.toml"
BASELINE = ROOT / "tests" / "sentry" / "baselines" / "serial-resources.json"


def collect_suite(
    root: Path, expr: str, dump_dir: Path, target: str = "tests"
) -> dict[str, list[dict[str, object]]]:
    """Collect `target` under `root` in a clean subprocess and return the dump plugin's report."""
    out = dump_dir / "dump.json"
    out.unlink(missing_ok=True)
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("PYTEST_") and k != "DECOY_TEST_ORDER_FILE"
    }
    env[res.DUMP_ENV] = str(out)
    env["PYTHONPATH"] = os.pathsep.join([str(HERE), env.get("PYTHONPATH", "")])
    proc = subprocess.run(  # noqa: S603 -- fixed argv, this interpreter
        [
            sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
            "-p", "no:xdist", "-p", "_resources", "-o", "addopts=", "-m", expr, target,
        ],
        cwd=root, env=env, capture_output=True, text=True, timeout=900, check=False,
    )  # fmt: skip
    assert out.exists(), f"collection wrote no dump (rc {proc.returncode}): {proc.stdout[-800:]}"
    data = json.loads(out.read_text())
    out.unlink()
    return data


def as_items(data: dict[str, list[dict[str, object]]]) -> list[res.Collected]:
    return [res.Collected(**i) for i in data["items"]]  # type: ignore[arg-type]


@pytest.fixture(scope="session")
def suite(tmp_path_factory: pytest.TempPathFactory) -> dict[str, list[dict[str, object]]]:
    expr = res.parse_marker_expr((ROOT / "scripts" / "dev" / "gates.sh").read_text())
    return collect_suite(ROOT, expr, tmp_path_factory.mktemp("serial-dump"))


def test_marker_expr_is_read_strictly_from_the_real_gates_line() -> None:
    text = (ROOT / "scripts" / "dev" / "gates.sh").read_text()
    line = next(ln for ln in text.splitlines() if ln.startswith("PYTEST_ARGS="))
    assert line == f"PYTEST_ARGS=(-m '{res.parse_marker_expr(text)}')"
    for changed in (
        line.replace("(-m '", "(-m x '"),  # an extra argument before the expression
        line.replace("')", "' --strict-markers)"),  # an extra argument after it
        line + " junk",
        line.replace("-m '", '-m "').replace("')", '")'),  # double quotes
        "PYTEST_ARGS=(-m '')",  # empty expression
        "# no line at all",
        line + "\n" + line,  # ambiguous: two definitions
    ):
        with pytest.raises(ValueError, match="PYTEST_ARGS"):
            res.parse_marker_expr(changed)


def unverifiable(data: dict[str, list[dict[str, object]]]) -> dict[str, str]:
    return {str(e["file"]): str(e["reason"]) for e in [*data["skipped"], *data["errors"]]}


def registry_ids(text: str) -> list[str]:
    return [row["id"] for row in tomllib.loads(text).get("serial", [])]


def allowlist() -> set[str]:
    return set(json.loads(BASELINE.read_text())["entries"])


def test_every_host_resource_test_is_serial_or_allowlisted(
    suite: dict[str, list[dict[str, object]]],
) -> None:
    scanner = res.Scanner(ROOT)

    def scan(item: res.Collected) -> frozenset[res.Hit]:
        path = ROOT / item.source
        if not item.source or not path.is_file():
            assert not item.qualname, f"no source file for {item.nodeid}"
            return frozenset()
        return scanner.hits(path, item.qualname)

    failures, stale = res.resource_failures(as_items(suite), scan, allowlist())
    assert failures == [], "\n".join(failures[:40])
    assert stale == [], "allowlist rows no longer needed (drop them):\n" + "\n".join(stale)


def test_the_required_serial_registry_is_marked(suite: dict[str, list[dict[str, object]]]) -> None:
    failures, unverified = res.unmarked_registry_entries(
        registry_ids(REGISTRY.read_text()), as_items(suite), unverifiable(suite)
    )
    # gates.sh sets DECOY_GATE_VENV_STRICT in its sentry step: that venv has every extra.
    problems = res.registry_problems(
        failures, unverified, bool(os.environ.get("DECOY_GATE_VENV_STRICT"))
    )
    assert problems == [], "\n".join(problems)


def test_strict_mode_turns_an_entry_whose_file_skips_at_collection_red() -> None:
    skipped = {"tests/x/test_gone.py": "could not import module"}
    failures, unverified = res.unmarked_registry_entries(
        ["tests/x/test_gone.py::test_a"], [], skipped
    )
    assert failures == [] and len(unverified) == 1
    assert res.registry_problems(failures, unverified, strict=False) == []
    strict = res.registry_problems(failures, unverified, strict=True)
    assert len(strict) == 1 and "tests/x/test_gone.py::test_a" in strict[0]


def test_the_registry_only_grows() -> None:
    base = subprocess.run(  # noqa: S603 -- fixed argv
        ["git", "-C", str(ROOT), "merge-base", "origin/main", "HEAD"],  # noqa: S607
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    if base.returncode != 0:
        pytest.skip("no origin/main merge base")
    old = subprocess.run(  # noqa: S603 -- fixed argv
        ["git", "-C", str(ROOT), "show", f"{base.stdout.strip()}:{REGISTRY.relative_to(ROOT)}"],  # noqa: S607
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    if old.returncode != 0:
        return  # the registry is new on this branch
    current = tomllib.loads(REGISTRY.read_text())
    cur = {
        "serial": [r["id"] for r in current.get("serial", [])],
        "removed": [f"{r['id']}|{r.get('reason', '')}" for r in current.get("removed", [])],
    }
    assert res.removed_without_reason(set(registry_ids(old.stdout)), cur) == []


# --- seeded bypasses: each spelling in the plan's mutation list is caught -------------------


def scan_text(tmp_path: Path, body: str, name: str = "test_x.py", qual: str | None = None):
    (tmp_path / name).write_text(body)
    scanner = res.Scanner(tmp_path)
    funcs = scanner.module(tmp_path / name).funcs
    names = [qual] if qual else [q for q, fn in funcs.items() if fn.name.startswith("test")]
    return frozenset().union(*(scanner.hits(tmp_path / name, q) for q in names))


@pytest.mark.parametrize(
    ("body", "kind"),
    [
        (
            "import time\ndef test_t():\n    t=time.perf_counter()\n    assert time.perf_counter()-t < 2.0\n",
            "clock",
        ),
        (
            "from time import perf_counter_ns as pc\ndef test_t():\n    t=pc()\n    elapsed=pc()-t\n    assert elapsed < 5\n",
            "clock",
        ),
        (
            "import time as tm\ndef test_t():\n    t0=tm.monotonic()\n    d=tm.monotonic()-t0\n    assert d < 1\n",
            "clock",
        ),
        (
            "import pytest\nBOUND_S=3\ndef test_t():\n    wall=1\n    if wall > BOUND_S:\n        pytest.fail('x')\n",
            "clock",
        ),
        (
            "import unittest\nclass T(unittest.TestCase):\n    def test_t(self):\n        elapsed=1\n        self.assertLess(elapsed, 1.0)\n",
            "clock",
        ),
        (
            "import resource\ndef test_t():\n    rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss\n    assert rss < 10\n",
            "memory",
        ),
        ("def test_t(ev):\n    assert ev.suspected_memory_pressure == 1\n", "memory"),
        ("import threading\ndef test_t():\n    assert threading.active_count() == 3\n", "threads"),
        (
            "import socket\ndef test_t():\n    s=socket.socket()\n    s.bind(('127.0.0.1', 8080))\n",
            "port",
        ),
        (
            "import resource\ndef test_t():\n    resource.setrlimit(resource.RLIMIT_AS, (1, 1))\n",
            "rlimit",
        ),
    ],
)
def test_the_scan_catches_each_spelling(tmp_path: Path, body: str, kind: str) -> None:
    assert kind in {h.kind for h in scan_text(tmp_path, body)}


def test_the_scan_ignores_a_child_rlimit_and_port_zero(tmp_path: Path) -> None:
    body = (
        "import resource, socket, subprocess\n"
        "def test_t():\n"
        "    subprocess.run(['x'], preexec_fn=lambda: resource.setrlimit(1, (1, 1)))\n"
        "    socket.socket().bind(('127.0.0.1', 0))\n"
    )
    assert scan_text(tmp_path, body) == frozenset()


def test_the_scan_follows_a_helper_module_and_a_local_call(tmp_path: Path) -> None:
    (tmp_path / "tests" / "connectors").mkdir(parents=True)
    (tmp_path / "tests" / "connectors" / "_x.py").write_text(
        "def finished(run):\n    assert run['wall'] < 3\n"
    )
    body = (
        "from tests.connectors import _x\n"
        "def _check(run):\n    _x.finished(run)\n"
        "def test_t():\n    _check({})\n"
    )
    assert scan_text(tmp_path, body, qual="test_t") != frozenset()


# --- judging: mutations of the marks and of the registry -----------------------------------


def item(
    nodeid: str, serial: bool, qual: str = "test_t", source: str = "tests/x.py"
) -> res.Collected:
    return res.Collected(nodeid, source, qual, serial)


def test_an_unmarked_registry_test_is_red() -> None:
    items = [item("tests/connectors/test_gcs_timeouts.py::test_a[1]", False)]
    fail, _ = res.unmarked_registry_entries(
        ["tests/connectors/test_gcs_timeouts.py::test_a"], items, {}
    )
    assert fail


def test_a_misspelled_registry_id_is_red() -> None:
    items = [item("tests/a.py::test_a", True)]
    fail, unverified = res.unmarked_registry_entries(["tests/a.py::test_aa"], items, {})
    assert fail and not unverified


def test_a_registry_id_in_a_skipped_file_is_unverified_not_green() -> None:
    fail, unverified = res.unmarked_registry_entries(
        ["tests/a.py::test_a"], [], {"tests/a.py": "needs x"}
    )
    assert not fail and unverified == ["tests/a.py::test_a: needs x"]


def test_a_dropped_serial_directory_is_red() -> None:
    items = [item("tests/perf/test_p.py::test_p", False)]
    fail, _ = res.unmarked_registry_entries(["tests/perf/"], items, {})
    assert fail


def test_a_stale_allowlist_row_is_reported() -> None:
    _fail, stale = res.resource_failures(
        [item("tests/a.py::t", True)], lambda _i: frozenset(), {"tests/x.py::gone"}
    )
    assert stale == ["tests/x.py::gone"]


def test_removing_a_registry_id_needs_a_reason() -> None:
    cur = {"serial": ["a"], "removed": ["b|"]}
    assert res.removed_without_reason({"a", "b"}, cur) == ["b"]
    cur = {"serial": ["a"], "removed": ["b|moved into a directory prefix"]}
    assert res.removed_without_reason({"a", "b"}, cur) == []


def test_a_conftest_pytestmark_does_not_mark_a_directory(tmp_path: Path) -> None:
    """A pytestmark in a conftest is ignored by pytest, so only the collection hook can mark."""
    d = tmp_path / "scratch"
    d.mkdir()
    (d / "conftest.py").write_text("import pytest\npytestmark = pytest.mark.serial\n")
    (d / "test_s.py").write_text("def test_s():\n    rss = 1\n    assert rss < 5\n")
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers = serial: x\n")
    (tmp_path / "out").mkdir()
    data = collect_suite(tmp_path, "not benchmark", tmp_path / "out", "scratch")
    assert [i["serial"] for i in data["items"]] == [False]

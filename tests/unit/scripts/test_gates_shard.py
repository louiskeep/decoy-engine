"""`gates.sh --shard I/N`: selection split, malformed input, and the guards that stop a green run
with zero tests. Shell shims on PATH stand in for git and sort so the failure paths are reachable."""

from __future__ import annotations

import os
import re
import shutil
import stat
from pathlib import Path

import pytest
from _gates_harness import BASH, GIT, diff_clone, seeded_clone
from _gates_harness import run as _run

pytestmark = pytest.mark.skipif(BASH is None or GIT is None, reason="needs bash and git")

GATES = [str(BASH), "scripts/dev/gates.sh"]
FILES = {f"tests/_shard/test_f{i}.py": "def test_ok():\n    assert True\n" for i in range(1, 6)}


def _shim(tmp_path: Path, name: str, body: str) -> dict[str, str]:
    """A `name` wrapper first on PATH; $REAL is the real binary's path."""
    real = shutil.which(name)
    assert real
    bindir = tmp_path / f"shim-{name}"
    bindir.mkdir()
    script = bindir / name
    script.write_text(f"#!/bin/bash\nREAL={real}\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return {"PATH": f"{bindir}:{os.environ['PATH']}"}


def _files_in(out: str) -> set[str]:
    return set(re.findall(r"test_f\d\.py", out))


def _shard(clone: Path, spec: str) -> set[str]:
    proc = _run([*GATES, "--steps", "tests", "--shard", spec, "tests/_shard"], clone)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    return _files_in(out)


@pytest.mark.parametrize("n", [2, 3])
def test_shards_cover_every_file_with_no_overlap(tmp_path: Path, n: int) -> None:
    clone = diff_clone(tmp_path, FILES)
    parts = [_shard(clone, f"{i}/{n}") for i in range(1, n + 1)]
    assert all(parts), parts
    assert set().union(*parts) == {Path(f).name for f in FILES}
    assert sum(len(p) for p in parts) == len(FILES), parts


def test_shard_deals_round_robin_over_the_sorted_files(tmp_path: Path) -> None:
    clone = diff_clone(tmp_path, FILES)
    assert _shard(clone, "1/2") == {"test_f1.py", "test_f3.py", "test_f5.py"}
    assert _shard(clone, "2/2") == {"test_f2.py", "test_f4.py"}


def test_full_mode_shards_split_the_whole_tests_directory(tmp_path: Path) -> None:
    clone = diff_clone(tmp_path, {"docs/records/shard-probe.md": "# probe\n"})
    seen = []
    for spec in ("1/2", "2/2"):
        proc = _run([*GATES, "--full", "--steps", "", "--shard", spec], clone)
        out = proc.stdout + proc.stderr
        m = re.search(r"full suite \(tests\); shard \d/2 \((\d+) of (\d+) file\(s\)\)", out)
        assert m, out
        seen.append((int(m[1]), int(m[2])))
    assert seen[0][1] == seen[1][1] > 100, seen
    assert seen[0][0] + seen[1][0] == seen[0][1], seen


@pytest.mark.parametrize("spec", ["1/2/3", "3/2", "0/2", "1/0", "a/2", "1/", "/2", "01/2", "1/2x"])
def test_malformed_shard_exits_2(tmp_path: Path, spec: str) -> None:
    clone = diff_clone(tmp_path, FILES)
    proc = _run([*GATES, "--steps", "tests", "--shard", spec, "tests/_shard"], clone)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "--shard" in proc.stderr


def test_shard_sorts_in_the_c_locale(tmp_path: Path) -> None:
    log = tmp_path / "sort-locale"
    shim = _shim(tmp_path, "sort", f'echo "${{LC_ALL-unset}}" >>{log}\nexec "$REAL" "$@"')
    clone = diff_clone(tmp_path, FILES)
    proc = _run(
        [*GATES, "--steps", "tests", "--shard", "1/2", "tests/_shard"],
        clone,
        LC_ALL="en_US.UTF-8",
        **shim,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    seen = set(log.read_text().split())
    assert seen == {"C"}, seen


def test_shard_fails_when_git_ls_files_fails(tmp_path: Path) -> None:
    # A masked git failure used to empty the file list, drop the tests step and pass green.
    shim = _shim(
        tmp_path, "git", '[ "$1" = ls-files ] && [ "$2" = -- ] && exit 128\nexec "$REAL" "$@"'
    )
    clone = diff_clone(tmp_path, FILES)
    proc = _run([*GATES, "--steps", "tests", "--shard", "1/2", "tests/_shard"], clone, **shim)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "git ls-files failed" in proc.stderr


def test_shard_fails_when_the_selection_holds_no_test_files(tmp_path: Path) -> None:
    clone = diff_clone(tmp_path, {"tests/_empty/helper.py": "X = 1\n"})
    proc = _run([*GATES, "--steps", "tests", "--shard", "1/2", "tests/_empty"], clone)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "selected 0 test files" in proc.stderr


def test_shard_with_fewer_files_than_shards_skips_the_empty_slice(tmp_path: Path) -> None:
    clone = diff_clone(tmp_path, {"tests/_one/test_f1.py": FILES["tests/_shard/test_f1.py"]})
    proc = _run([*GATES, "--steps", "tests", "--shard", "2/2", "tests/_one"], clone)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "has no test files" in proc.stdout


def test_baseline_shard_with_only_main_shared_failures_is_green(tmp_path: Path) -> None:
    # The tests step exits 1 on a failure main also has; that step still ran, so not exit 3.
    clone = seeded_clone(tmp_path)
    argv = [*GATES, "--steps", "tests", "--baseline", "--shard", "1/1"]
    proc = _run([*argv, "tests/_seeded/test_seed_shared.py"], clone)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert re.search(r"BOTH\s+tests/_seeded/test_seed_shared\.py::test_shared", out), out

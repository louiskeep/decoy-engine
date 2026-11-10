"""changed_tests selects a test that imports a changed module as `from pkg import mod`."""

from __future__ import annotations

from pathlib import Path

import pytest
from _gates_harness import BASH, GIT, diff_clone, gates, git, pytest_argvs, recording_venv

pytestmark = pytest.mark.skipif(BASH is None or GIT is None, reason="needs bash and git")

SRC = "src/zzpkg/_zzmod.py"
TEST_OK = "def test_ok():\n    pass\n"


def _selected(tmp_path: Path, files: dict[str, str]) -> set[str]:
    # The importing tests are already on main; only the src module changes on the branch. A test
    # file in the diff would be selected for being changed, which proves nothing.
    clone = diff_clone(tmp_path, files)
    git(clone, "update-ref", "refs/remotes/origin/main", "HEAD")
    (clone / SRC).parent.mkdir(parents=True, exist_ok=True)
    (clone / SRC).write_text("X = 1\n")
    git(clone, "add", "-A")
    git(clone, "commit", "-qm", "src")
    venv, log = recording_venv(tmp_path)
    rc, out = gates(clone, venv, "--steps", "tests", GATES_WORKERS="1")
    assert rc == 0, out
    (argv,) = [a for a in pytest_argvs(log) if any(x.endswith(".py") for x in a)]
    return {x for x in argv if x.startswith("tests/")}


@pytest.mark.parametrize(
    "body",
    [
        "from zzpkg import _zzmod\n",
        "from zzpkg import _zzmod as m\n",
        "from zzpkg import (\n    other,\n    _zzmod,\n)\n",
        "from  zzpkg  import  other, _zzmod\n",
    ],
)
def test_from_import_of_changed_module_is_selected(tmp_path: Path, body: str) -> None:
    sel = _selected(tmp_path, {"tests/_x/test_user.py": body + TEST_OK})
    assert "tests/_x/test_user.py" in sel


def test_dotted_import_still_selected(tmp_path: Path) -> None:
    sel = _selected(tmp_path, {"tests/_x/test_dot.py": "import zzpkg._zzmod\n" + TEST_OK})
    assert "tests/_x/test_dot.py" in sel


def test_unrelated_from_import_not_selected(tmp_path: Path) -> None:
    sel = _selected(tmp_path, {"tests/_x/test_other.py": "from zzpkg import other\n" + TEST_OK})
    assert "tests/_x/test_other.py" not in sel

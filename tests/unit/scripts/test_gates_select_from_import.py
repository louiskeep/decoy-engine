"""changed_tests selects the tests that import a changed file, however the import is spelled."""

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


def _select_changed(tmp_path: Path, base: dict[str, str], changed: dict[str, str]) -> set[str]:
    """Selection when `base` is on main and `changed` is the whole branch diff."""
    clone = diff_clone(tmp_path, base)
    git(clone, "update-ref", "refs/remotes/origin/main", "HEAD")
    for rel, body in changed.items():
        (clone / rel).parent.mkdir(parents=True, exist_ok=True)
        (clone / rel).write_text(body)
    git(clone, "add", "-A")
    git(clone, "commit", "-qm", "change")
    venv, log = recording_venv(tmp_path)
    rc, out = gates(clone, venv, "--steps", "tests", GATES_WORKERS="1")
    assert rc == 0, out
    (argv,) = [a for a in pytest_argvs(log) if any(x.endswith(".py") for x in a)]
    return {x for x in argv if x.startswith("tests/")}


HELPER = "tests/_zz/_helper.py"
USER = "tests/_zz/test_user.py"
OTHER = "tests/_zz/test_other.py"


def test_mixed_diff_selects_helper_dependent(tmp_path: Path) -> None:
    # The Codex regression: a changed test plus a changed helper. The helper's importer is not
    # in the diff and imports the helper without `.py`.
    base = {
        HELPER: "X = 1\n",
        USER: "from _helper import X\n" + TEST_OK,
        "tests/_zz/test_changed.py": TEST_OK,
    }
    sel = _select_changed(
        tmp_path, base, {HELPER: "X = 2\n", "tests/_zz/test_changed.py": TEST_OK + "# edit\n"}
    )
    assert USER in sel
    assert "tests/_zz/test_changed.py" in sel


@pytest.mark.parametrize(
    "body",
    [
        "from ._helper import X\n",
        "from . import _helper\n",
        "from . import (\n    other,\n    _helper,\n)\n",
        "from \\\n    ._helper import X\n",
        "import tests._zz._helper\n",
        "from tests._zz import _helper\n",
    ],
)
def test_helper_import_spellings_are_selected(tmp_path: Path, body: str) -> None:
    base = {
        "tests/__init__.py": "",
        "tests/_zz/__init__.py": "",
        HELPER: "X = 1\n",
        USER: body + TEST_OK,
        OTHER: "import os\n" + TEST_OK,
    }
    sel = _select_changed(tmp_path, base, {HELPER: "X = 2\n"})
    assert USER in sel
    assert OTHER not in sel


def test_transitive_helper_import_is_selected(tmp_path: Path) -> None:
    base = {
        HELPER: "X = 1\n",
        "tests/_zz/_mid.py": "from _helper import X\n",
        USER: "import _mid\n" + TEST_OK,
    }
    assert USER in _select_changed(tmp_path, base, {HELPER: "X = 2\n"})


def test_conftest_change_selects_tests_below_it(tmp_path: Path) -> None:
    base = {"tests/_zz/conftest.py": "", USER: TEST_OK, "tests/_yy/test_far.py": TEST_OK}
    sel = _select_changed(tmp_path, base, {"tests/_zz/conftest.py": "# edit\n"})
    assert USER in sel
    assert "tests/_yy/test_far.py" not in sel


def test_unparseable_test_file_is_selected(tmp_path: Path) -> None:
    sel = _selected(tmp_path, {"tests/_x/test_broken.py": "def (:\n"})
    assert "tests/_x/test_broken.py" in sel


def test_unrelated_helper_dependent_not_selected(tmp_path: Path) -> None:
    base = {HELPER: "X = 1\n", OTHER: "import os\n" + TEST_OK}
    assert OTHER not in _select_changed(tmp_path, base, {HELPER: "X = 2\n"})


def test_lazy_import_in_src_importer_selects_tests_of_the_importer(tmp_path: Path) -> None:
    # Live shape (#464): the changed src module is imported only inside function bodies of a
    # sibling src module, and tests import that sibling.
    clone_files = {
        "src/zzpkg/__init__.py": "",
        "src/zzpkg/synth.py": (
            "def run():\n    from ._leaf import X\n    return X\n\n"
            "def other():\n    from zzpkg._leaf import X\n    return X\n"
        ),
        "src/zzpkg/_mid.py": "def f():\n    import zzpkg.synth\n",
        USER: "from zzpkg import _mid\n" + TEST_OK,
        OTHER: "import os\n" + TEST_OK,
    }
    sel = _select_changed(
        tmp_path, clone_files | {"src/zzpkg/_leaf.py": "X = 1\n"}, {"src/zzpkg/_leaf.py": "X = 2\n"}
    )
    assert USER in sel
    assert OTHER not in sel

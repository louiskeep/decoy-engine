"""scripts/dev/select_tests.py: tests are selected through transitive importers, not only by name."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

DEV = Path(__file__).resolve().parents[3] / "scripts" / "dev"
sys.path.insert(0, str(DEV))
import select_tests as sel  # noqa: E402  (path set up above)

PKG = "src/decoy_engine"


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    _write(
        tmp_path,
        {
            f"{PKG}/__init__.py": "",
            f"{PKG}/quality/__init__.py": "",
            f"{PKG}/quality/fidelity.py": "X = 1\n",
            # Function-level import, as execution/_pipeline_finalize does for quality.
            f"{PKG}/execution/__init__.py": "",
            f"{PKG}/execution/finalize.py": "def f():\n    from decoy_engine.quality import fidelity\n",
            # Relative import of a sibling.
            f"{PKG}/execution/route.py": "from . import finalize\n",
            # Second hop: imports route only.
            f"{PKG}/api.py": "from decoy_engine.execution.route import x\n",
            f"{PKG}/other.py": "Y = 2\n",
            "tests/__init__.py": "",
            "tests/test_direct.py": "from decoy_engine.quality.fidelity import X\n",
            "tests/test_function_level.py": "import decoy_engine.execution.finalize\n",
            "tests/test_relative_hop.py": "from decoy_engine.execution import route\n",
            "tests/test_two_hop.py": "import decoy_engine.api\n",
            "tests/test_text_only.py": "def test():\n    target = 'decoy_engine.quality.fidelity.X'\n",
            "tests/test_unrelated.py": "from decoy_engine import other\n",
        },
    )
    return tmp_path


def _pick(root: Path, changed: str) -> set[str]:
    return set(sel.select(root, [changed]))


def test_function_level_import_links_importer(tree: Path) -> None:
    picked = _pick(tree, f"{PKG}/quality/fidelity.py")
    assert "tests/test_function_level.py" in picked  # reaches fidelity via finalize's inner import


def test_relative_import_links_importer(tree: Path) -> None:
    picked = _pick(tree, f"{PKG}/execution/finalize.py")
    assert "tests/test_relative_hop.py" in picked  # route does `from . import finalize`


def test_two_hop_chain(tree: Path) -> None:
    picked = _pick(tree, f"{PKG}/quality/fidelity.py")
    # fidelity <- finalize <- route <- api <- test_two_hop
    assert "tests/test_two_hop.py" in picked
    assert "tests/test_direct.py" in picked


def test_text_mention_still_selects(tree: Path) -> None:
    assert "tests/test_text_only.py" in _pick(tree, f"{PKG}/quality/fidelity.py")


def test_unrelated_test_not_selected(tree: Path) -> None:
    picked = _pick(tree, f"{PKG}/quality/fidelity.py")
    assert "tests/test_unrelated.py" not in picked
    assert _pick(tree, f"{PKG}/other.py") == {"tests/test_unrelated.py"}


def test_test_helper_imports_propagate(tree: Path) -> None:
    _write(
        tree,
        {
            "tests/_helper.py": "from decoy_engine.quality import fidelity\n",
            "tests/test_via_helper.py": "from _helper import thing\n",
        },
    )
    assert "tests/test_via_helper.py" in _pick(tree, f"{PKG}/quality/fidelity.py")


def test_unparseable_source_fails_closed(tree: Path) -> None:
    _write(tree, {f"{PKG}/broken.py": "def (:\n"})
    proc = subprocess.run(  # noqa: S603 -- fixed argv, tmp tree
        [sys.executable, str(DEV / "select_tests.py"), str(tree), f"{PKG}/other.py"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert proc.stdout == ""

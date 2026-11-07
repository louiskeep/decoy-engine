"""The collection guard refuses test ids that embed a memory address."""

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import _refuse_unstable_ids

ROOT = Path(__file__).resolve().parents[3]


def test_address_in_an_id_is_refused() -> None:
    items = [SimpleNamespace(nodeid="t.py::f[<list_iterator object at 0x1186e4c70>]")]
    with pytest.raises(pytest.UsageError, match="0x1186e4c70"):
        _refuse_unstable_ids(items)  # type: ignore[arg-type]


def test_plain_ids_pass() -> None:
    items = [SimpleNamespace(nodeid="t.py::f[iterator]"), SimpleNamespace(nodeid="t.py::g[0x10]")]
    _refuse_unstable_ids(items)  # type: ignore[arg-type]


def test_collection_hook_applies_the_guard(tmp_path: Path) -> None:
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        "import pytest\n\n"
        "@pytest.mark.parametrize('v', [object()], ids=repr)\n"
        "def test_x(v):\n    pass\n",
        encoding="utf-8",
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT), str(ROOT / "src")])}
    env.pop("PYTEST_ADDOPTS", None)
    run = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "tests.conftest",
            "--rootdir",
            str(tmp_path),
            str(probe),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert run.returncode == pytest.ExitCode.USAGE_ERROR, run.stdout + run.stderr
    assert "memory address" in run.stderr

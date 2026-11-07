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
    items = [
        SimpleNamespace(nodeid="t.py::f[<list_iterator object at 0x1186e4c70>]"),
        SimpleNamespace(nodeid="t.py::g[<function f at 0x7f3a>]"),
        SimpleNamespace(nodeid="t.py::h[<function <lambda> at 0x7f3b>]"),
        SimpleNamespace(nodeid="t.py::k[<function outer.<locals>.f at 0x7f3c>]"),
    ]
    with pytest.raises(pytest.UsageError) as refused:
        _refuse_unstable_ids(items)  # type: ignore[arg-type]
    assert "0x1186e4c70" in str(refused.value)
    assert "0x7f3a" in str(refused.value)
    assert "0x7f3b" in str(refused.value)
    assert "0x7f3c" in str(refused.value)


def test_plain_ids_pass() -> None:
    items = [
        SimpleNamespace(nodeid="t.py::f[iterator]"),
        SimpleNamespace(nodeid="t.py::g[0x10]"),
        SimpleNamespace(nodeid="t.py::h[located at 0xdeadbeef]"),
    ]
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

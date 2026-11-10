"""The impact-selected gate always adds the pinned tests of scripts/dev/gate_pins.txt."""

from __future__ import annotations

from pathlib import Path

import pytest
from _gates_harness import BASH, GIT, diff_clone, gates, pytest_argvs, recording_venv

pytestmark = pytest.mark.skipif(BASH is None or GIT is None, reason="needs bash and git")

REPO = Path(__file__).resolve().parents[3]
PINS = REPO / "scripts" / "dev" / "gate_pins.txt"


def _pinned() -> list[str]:
    lines = (ln.split("#")[0].strip() for ln in PINS.read_text().splitlines())
    return sorted(ln for ln in lines if ln)


def _tests_argv(log: Path) -> list[str]:
    (argv,) = [a for a in pytest_argvs(log) if any(x.endswith(".py") for x in a)]
    return [x for x in argv if x.startswith("tests/")]


def test_selected_run_unions_in_every_pin(tmp_path: Path) -> None:
    clone = diff_clone(tmp_path, {"tests/_x/test_probe.py": "def test_ok():\n    pass\n"})
    venv, log = recording_venv(tmp_path)
    rc, out = gates(clone, venv, "--steps", "tests", GATES_WORKERS="1")
    assert rc == 0, out
    ran = _tests_argv(log)
    assert "tests/_x/test_probe.py" in ran
    assert set(_pinned()) <= set(ran), set(_pinned()) - set(ran)
    assert f"+ always-on pins, {len(_pinned()) + 1} in all" in out


def test_docs_only_diff_stays_not_applicable(tmp_path: Path) -> None:
    clone = diff_clone(tmp_path, {"docs/records/pins-probe.md": "# probe\n"})
    venv, _ = recording_venv(tmp_path)
    rc, out = gates(clone, venv, "--steps", "format,tests")
    assert rc == 0, out
    assert "not applicable: the diff touches no code paths" in out


def test_missing_pin_fails_the_gate(tmp_path: Path) -> None:
    clone = diff_clone(tmp_path, {"tests/_x/test_probe.py": "def test_ok():\n    pass\n"})
    pins = clone / "scripts" / "dev" / "gate_pins.txt"
    pins.write_text(pins.read_text() + "tests/unit/absent_pin.py\n")
    venv, _ = recording_venv(tmp_path)
    rc, out = gates(clone, venv, "--steps", "tests")
    assert rc == 2 and "pinned test path not found: tests/unit/absent_pin.py" in out, out

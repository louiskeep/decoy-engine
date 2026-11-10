"""The always-on gate pin list cannot rot (owner decision 2026-11-10, GATE-SELECT option C).

scripts/dev/gates.sh unions scripts/dev/gate_pins.txt into every impact-selected run. A path
that was renamed or deleted would make every PR gate exit 2, or, if the reader ever skipped
missing paths, silently drop the pin. This sentry fails first and names the path.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PINS = ROOT / "scripts" / "dev" / "gate_pins.txt"
GATES = ROOT / "scripts" / "dev" / "gates.sh"


def _paths() -> list[str]:
    lines = (ln.split("#")[0].strip() for ln in PINS.read_text().splitlines())
    return [ln for ln in lines if ln]


def test_list_is_not_empty() -> None:
    assert _paths(), "gate_pins.txt lists no test; the always-on set would be empty"


def test_every_pinned_path_exists_and_is_a_test() -> None:
    bad = []
    for p in _paths():
        target = ROOT / p
        if target.is_file():
            ok = target.name.startswith("test_") and target.suffix == ".py"
        else:
            ok = target.is_dir() and any(target.rglob("test_*.py"))
        if not ok:
            bad.append(p)
    assert not bad, f"gate_pins.txt names missing or non-test paths: {bad}"


def test_no_duplicates() -> None:
    paths = _paths()
    assert len(paths) == len(set(paths)), "gate_pins.txt repeats a path"


def test_gates_reads_the_list() -> None:
    assert "PINS_FILE=scripts/dev/gate_pins.txt" in GATES.read_text()

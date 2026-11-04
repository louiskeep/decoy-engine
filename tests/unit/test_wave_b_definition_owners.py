"""Wave B definition owners (H-1 plan AT-1, "Engine definition owners").

Class 9 (behavior-preserving refactor drift): a move that leaves a definition behind,
defines it twice, or lands it in the wrong sibling. A slice's moved names live in their
``from`` file until the slice flips ``landed`` in its own PR, and in their planned file
after; names under ``stays`` never leave their file. A definition is a module-level
``def``, ``class``, ``Assign`` target or valued ``AnnAssign`` target, read with ``ast``;
import bindings never count, so a kept module importing a moved name is not a second owner.
"""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "h1" / "definition_owners.json").read_text())


def definitions(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    names: set[str] = set()
    for stmt in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(stmt.name)
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value is not None:
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            names |= {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}
    return names


def _family(fixture: dict) -> dict[str, list[str]]:
    """Each source file with its planned owner files, in a stable order."""
    family: dict[str, set[str]] = {}
    for key, part in fixture.items():
        if key != "stays":
            family.setdefault(part["from"], set()).update(part["owners"].values())
    return {src: [src, *sorted(owners)] for src, owners in family.items()}


def owner_violations(fixture: dict, root: Path) -> list[str]:
    family, out = _family(fixture), []
    expected = [
        (key, src, name, owner if part["landed"] else src)
        for key, part in fixture.items()
        if key != "stays"
        for src in [part["from"]]
        for name, owner in part["owners"].items()
    ]
    expected += [
        ("stays", src, name, src) for src, names in fixture["stays"].items() for name in names
    ]
    for key, src, name, want in expected:
        holders = [f for f in family[src] if name in definitions(root / f)]
        if holders != [want]:
            out.append(f"{key}: {name} defined in {holders or 'no file'}, expected {want}")
    return out


def coverage_gaps(fixture: dict, root: Path) -> list[str]:
    out = []
    for src, files in _family(fixture).items():
        moved = {
            n for k, p in fixture.items() if k != "stays" and p["from"] == src for n in p["owners"]
        }
        listed = moved | set(fixture["stays"].get(src, []))
        found = set().union(*(definitions(root / f) for f in files))
        out += [f"{src}: {n} not in the fixture" for n in sorted(found - listed)]
        out += [f"{src}: {n} in the fixture, defined nowhere" for n in sorted(listed - found)]
    return out


def test_every_moved_name_has_its_planned_owner():
    assert owner_violations(FIXTURE, ROOT) == []


def test_fixture_covers_every_top_level_definition():
    assert coverage_gaps(FIXTURE, ROOT) == []
    seeded = copy.deepcopy(FIXTURE)
    seeded["stays"]["src/decoy_engine/detection/detectors.py"].pop()
    assert coverage_gaps(seeded, ROOT)

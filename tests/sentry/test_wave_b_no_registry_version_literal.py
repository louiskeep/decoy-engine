"""No Wave B test pins a reason-registry version (H-1 plan 8.2).

Class 10 (frozen contract drift): ``REGISTRY_VERSION`` moves whenever a branch adds reason
identities, so a move slice's test that pins it turns red for a reason unrelated to the
move. Tests that need the version read ``decoy_engine.reasons.REGISTRY_VERSION`` at run
time. The files checked are the E0 seed list plus the ``tests`` list of every Wave B
manifest under ``docs/plans/h1-wave-b/manifests/``.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MANIFESTS = ROOT / "docs" / "plans" / "h1-wave-b" / "manifests"
MUTANT = ROOT / "tests" / "fixtures" / "h1" / "registry_version_mutant.py.txt"
E0_TEST_FILES = (
    "tests/unit/test_check_pure_move.py",
    "tests/unit/test_check_pure_move_scope.py",
    "tests/sentry/test_engine_string_targets.py",
    "tests/sentry/test_wave_b_no_registry_version_literal.py",
    "tests/sentry/test_reason_baseline_frozen.py",
    "tests/unit/test_wave_b_definition_owners.py",
)
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_CONTEXT = (ast.Call, ast.Compare, ast.Assign, ast.AnnAssign, ast.AugAssign, ast.keyword)
_WORDS = ("registry", "since")


def manifest_tests(path: Path) -> list[str]:
    """The ``tests`` list of one Wave B manifest; a manifest without one is refused."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if "tests" not in data:
        raise ValueError(f"{path.name}: Wave B manifest has no tests key")
    return list(data["tests"])


def listed_test_files() -> list[str]:
    manifests = sorted(MANIFESTS.glob("*.json")) if MANIFESTS.is_dir() else []
    return sorted({*E0_TEST_FILES, *(t for m in manifests for t in manifest_tests(m))})


def version_pins(source: str, filename: str = "<src>") -> list[str]:
    """``file:line`` of each version literal whose enclosing call, comparison or
    assignment mentions the registry or ``since``."""
    tree = ast.parse(source, filename=filename)
    parent = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    pins = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and _VERSION.match(str(node.value))):
            continue
        context = parent.get(node)
        while context is not None and not isinstance(context, _CONTEXT):
            context = parent.get(context)
        text = ast.unparse(context).lower() if context is not None else ""
        if any(word in text for word in _WORDS):
            pins.append(f"{filename}:{node.lineno}")
    return pins


def test_wave_b_tests_name_no_registry_version():
    missing = [rel for rel in listed_test_files() if not (ROOT / rel).is_file()]
    pins = [
        pin
        for rel in listed_test_files()
        if (ROOT / rel).is_file()
        for pin in version_pins((ROOT / rel).read_text(encoding="utf-8"), rel)
    ]
    assert not missing, f"listed Wave B test files are missing: {missing}"
    assert not pins, f"registry version pinned at: {pins}"


def test_sentry_flags_a_pinned_registry_version():
    assert version_pins(MUTANT.read_text(encoding="utf-8"), MUTANT.name) == [f"{MUTANT.name}:3"]


def test_manifest_without_tests_key_is_rejected(tmp_path):
    manifest = tmp_path / "e1-1.json"
    manifest.write_text(json.dumps({"old": "src/x.py", "new": [], "map": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="no tests key"):
        manifest_tests(manifest)

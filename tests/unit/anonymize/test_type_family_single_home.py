"""``type_family`` is defined once, in ``anonymize/_values.py``, and both packages use it."""

from __future__ import annotations

import ast
from pathlib import Path

import decoy_engine
import decoy_engine.anonymize as anonymize
from decoy_engine.quality.anonymity import _classes


def test_one_definition_in_the_engine() -> None:
    root = Path(decoy_engine.__file__).parent
    found = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "type_family"
            ):
                found.append(path.relative_to(root).as_posix())
    assert found == ["anonymize/_values.py"]


def test_quality_uses_the_public_function() -> None:
    assert _classes.type_family is anonymize.type_family

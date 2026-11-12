"""G-RESULT-ONE-CONSTRUCTOR (AT-E2): only ``run_result/_build.py`` builds a ``RunResult``.

The walk parses the engine tree. ``RunResult(...)`` is flagged anywhere. Inside ``run_result/``
every ``model_construct``, ``model_validate`` and ``model_copy`` call is flagged outside
``_build.py``, because a copy with ``update=`` skips validation and so skips the value-free
types. ``RunResultRead`` is exempt in ``_model.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

PKG = Path(__file__).resolve().parents[2] / "src" / "decoy_engine"
BUILDER = "run_result/_build.py"
GUARDED_METHODS = {"model_construct", "model_validate", "model_copy"}


def _receiver(node: ast.expr) -> str:
    return node.id if isinstance(node, ast.Name) else ""


def violations(root: Path) -> list[str]:
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel == BUILDER:
            continue
        in_pkg = rel.startswith("run_result/")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id == "RunResult":
                found.append(f"{rel}:{node.lineno} RunResult(")
            elif isinstance(func, ast.Attribute):
                if func.attr == "RunResult":
                    found.append(f"{rel}:{node.lineno} RunResult(")
                elif func.attr in GUARDED_METHODS:
                    if _receiver(func.value) == "RunResult":
                        found.append(f"{rel}:{node.lineno} RunResult.{func.attr}")
                    elif in_pkg and not (
                        rel == "run_result/_model.py" and _receiver(func.value) == "RunResultRead"
                    ):
                        found.append(f"{rel}:{node.lineno} .{func.attr}")
    return found


def test_only_the_builder_constructs_a_run_result() -> None:
    assert violations(PKG) == []


def test_walk_sees_a_seeded_violation(tmp_path: Path) -> None:
    (tmp_path / "run_result").mkdir()
    (tmp_path / "run_result" / "_project.py").write_text(
        "def f(x):\n    return x.model_copy(update={'a': 1})\n", encoding="utf-8"
    )
    (tmp_path / "elsewhere.py").write_text(
        "from m import RunResult\nRunResult(a=1)\nRunResult.model_construct()\n", encoding="utf-8"
    )
    (tmp_path / "run_result" / "_build.py").write_text("x = RunResult(a=1)\n", encoding="utf-8")
    found = violations(tmp_path)
    assert len(found) == 3
    assert any("_project.py" in v for v in found)
    assert not any("_build.py" in v for v in found)

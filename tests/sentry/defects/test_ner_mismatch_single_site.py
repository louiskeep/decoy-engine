"""`NER_MODEL_VERSION_MISMATCH` is raised from one function with one message.

`_strategies/_ner_config.enforce_ner_model_version` owns the raise. Any other mention of the code
in the source (the symbol through `reasons`, the symbol imported or aliased, or the literal text)
is a second raise site. The allowlist keys on `(path, function)` and fails when an entry no longer
matches a real site.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[3] / "src" / "decoy_engine"
SYMBOL = "NER_MODEL_VERSION_MISMATCH"
LITERAL = "ner_model_version_mismatch"
ALLOWED = {("execution/_strategies/_ner_config.py", "enforce_ner_model_version")}


class _Sites(ast.NodeVisitor):
    def __init__(self) -> None:
        self.stack: list[str] = []
        self.found: list[str] = []

    def _scope(self, node: ast.AST, name: str) -> None:
        self.stack.append(name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope(node, node.name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scope(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._scope(node, node.name)

    def _hit(self) -> None:
        self.found.append(".".join(self.stack))

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == SYMBOL:
            self._hit()
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id == SYMBOL:
            self._hit()

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if any(a.name == SYMBOL for a in node.names):
            self._hit()

    def visit_Constant(self, node: ast.Constant) -> None:
        if node.value == LITERAL:
            self._hit()

    def visit_Expr(self, node: ast.Expr) -> None:
        # A docstring or bare string statement is prose, not a raise.
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return
        self.generic_visit(node)


def sites(source: str) -> list[str]:
    visitor = _Sites()
    visitor.visit(ast.parse(source))
    return visitor.found


def _real_sites() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel.startswith("reasons/"):
            continue
        found.update((rel, fn) for fn in sites(path.read_text(encoding="utf-8")))
    return found


def test_only_the_helper_names_the_code() -> None:
    assert _real_sites() == ALLOWED


def test_every_allowlist_entry_matches_a_real_site() -> None:
    assert _real_sites() >= ALLOWED


@pytest.mark.parametrize(
    "source",
    [
        "from decoy_engine import reasons\n"
        "def f():\n    raise E(code=reasons.runtime.NER_MODEL_VERSION_MISMATCH)\n",
        "from decoy_engine.reasons import runtime as r\n"
        "def f():\n    raise E(code=r.NER_MODEL_VERSION_MISMATCH)\n",
        "from decoy_engine.reasons.runtime import NER_MODEL_VERSION_MISMATCH as C\n"
        "def f():\n    raise E(code=C)\n",
        "CODE = reasons.runtime.NER_MODEL_VERSION_MISMATCH\ndef f():\n    raise E(code=CODE)\n",
        "def f():\n    raise E(code='ner_model_version_mismatch')\n",
    ],
)
def test_the_scan_flags_each_seeded_spelling(source: str) -> None:
    assert sites(source) != []


def test_prose_naming_the_code_is_not_a_site() -> None:
    assert sites('def f():\n    """Raises ner_model_version_mismatch."""\n    return 1\n') == []

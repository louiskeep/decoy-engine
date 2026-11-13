"""DUP-DEFECTS E1 sentry: snapshot bytes are parsed only by `parse_snapshot_json`.

A second `json.loads` over snapshot bytes would bring back the raw `UnicodeDecodeError` that
carries the file. The walk covers the modules that hold snapshot bytes; each legitimate
`json.loads` of something else is listed by (file, function) with its reason. The list is exact.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / "src" / "decoy_engine"
OWNER = "generation/statistical/_snapshot_io.py"
SCANNED = (
    "generation/statistical",
    "generation/_plan_entry.py",
    "plan/_generation.py",
    "plan/_serialize.py",
)
# (file, function) -> number of calls. Each decodes `config_json`, never snapshot bytes. A count,
# not a flag, so a second call in the same function (a snapshot parse) still fails.
ALLOWED = {
    ("generation/_plan_entry.py", "_prepare_generation"): 1,
    ("plan/_serialize.py", "_generation_plan_from_dict"): 1,
}


def _files() -> list[Path]:
    out: list[Path] = []
    for entry in SCANNED:
        path = SRC / entry
        out.extend(sorted(path.rglob("*.py")) if path.is_dir() else [path])
    return [p for p in out if p.relative_to(SRC).as_posix() != OWNER]


class _Sites(ast.NodeVisitor):
    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.stack: list[str] = []
        self.sites: Counter[tuple[str, str]] = Counter()

    def _scope(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _scope  # noqa: N815
    visit_AsyncFunctionDef = _scope  # noqa: N815

    def visit_Call(self, node: ast.Call) -> None:
        f = node.func
        if (
            isinstance(f, ast.Attribute)
            and f.attr in {"loads", "load"}
            and isinstance(f.value, ast.Name)
            and f.value.id == "json"
        ):
            self.sites[(self.rel, self.stack[-1] if self.stack else "<module>")] += 1
        self.generic_visit(node)


def _json_loads_sites() -> Counter[tuple[str, str]]:
    sites: Counter[tuple[str, str]] = Counter()
    for path in _files():
        visitor = _Sites(path.relative_to(SRC).as_posix())
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        sites += visitor.sites
    return sites


def test_only_the_snapshot_io_owner_parses_snapshot_bytes() -> None:
    assert _json_loads_sites() == ALLOWED

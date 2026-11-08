"""A9: structural guards. Only the gate's caller builds a `PairScore`; the report reads only public
`quality` modules; `quality` never imports `generation` (the edge stays one-way). Each scanner is
shown to see a seeded violation of every form it claims to catch."""

from __future__ import annotations

import ast
import shutil
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[5] / "src" / "decoy_engine"
_COPULA = _SRC / "generation" / "copula"
_QUALITY = _SRC / "quality"


def _report_modules(root: Path) -> list[Path]:
    return sorted(root.glob("_quality_*.py"))


def _imported_modules(tree: ast.AST, package: str) -> list[tuple[int, str]]:
    """Every module an import names, absolute or relative, plus importlib string literals."""
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")[: len(package.split(".")) - node.level + 1]
                base = ".".join([*parts, base] if base else parts)
            out.append((node.lineno, base))
            out.extend((node.lineno, f"{base}.{alias.name}") for alias in node.names)
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            first = node.args[0]
            if name in {"import_module", "__import__"} and isinstance(first, ast.Constant):
                out.append((node.lineno, str(first.value)))
    return out


def _aliases(tree: ast.Module) -> tuple[set[str], set[str], set[str]]:
    """Names bound to the PairScore class, to the dataclasses module, and to dataclasses.replace,
    through any import form or alias."""
    pair_score, dataclasses_module, replace = {"PairScore"}, set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "dataclasses":
                    dataclasses_module.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "PairScore":
                    pair_score.add(alias.asname or alias.name)
                if node.module == "dataclasses" and alias.name == "replace":
                    replace.add(alias.asname or alias.name)
    return pair_score, dataclasses_module, replace


def _annotation_nodes(tree: ast.Module) -> set[int]:
    """ids of every node inside a type annotation (a type named there builds nothing)."""
    roots: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            roots.append(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.returns is not None:
                roots.append(node.returns)
            args = node.args
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
                if arg is not None and arg.annotation is not None:
                    roots.append(arg.annotation)
    return {id(n) for root in roots for n in ast.walk(root)}


def scan_pair_score_builders(root: Path) -> list[str]:
    """Any use of PairScore (by any alias, called or bound to another name) outside
    `_quality_pairwise.score_pairs` and type annotations; any dataclasses.replace by any alias;
    any __setattr__ call."""
    return [
        f for path in _report_modules(root) for f in _scan_builders(path.name, path.read_text())
    ]


def _scan_builders(filename: str, source: str) -> list[str]:
    found: list[str] = []
    tree = ast.parse(source)
    pair_score, dataclasses_module, replace = _aliases(tree)
    annotations = _annotation_nodes(tree)

    def visit(node: ast.AST, function: str | None) -> None:
        where = f"{filename}:{getattr(node, 'lineno', 0)}"
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.iter_child_nodes(node):
                visit(child, node.name)
            return
        if isinstance(node, ast.ClassDef) and node.name == "PairScore":
            return  # the definition itself
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return
        in_score_pairs = filename == "_quality_pairwise.py" and function == "score_pairs"
        if id(node) not in annotations and not in_score_pairs:
            if isinstance(node, ast.Name) and node.id in pair_score:
                found.append(f"{where} PairScore used outside score_pairs")
            if isinstance(node, ast.Attribute) and node.attr == "PairScore":
                found.append(f"{where} PairScore used outside score_pairs")
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in replace:
                found.append(f"{where} dataclasses.replace")
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "replace"
                and isinstance(func.value, ast.Name)
                and func.value.id in dataclasses_module
            ):
                found.append(f"{where} dataclasses.replace")
            if isinstance(func, ast.Attribute) and func.attr == "__setattr__":
                found.append(f"{where} __setattr__")
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(tree, None)
    return found


def scan_private_quality_imports(root: Path) -> list[str]:
    package = "decoy_engine.generation.copula"
    return [
        f"{path.name}:{line} {name}"
        for path in _report_modules(root)
        for line, name in _imported_modules(ast.parse(path.read_text()), package)
        if name.startswith("decoy_engine.quality.")
        and any(part.startswith("_") for part in name.split(".")[2:])
    ]


def scan_quality_to_generation(root: Path) -> list[str]:
    found = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).with_suffix("")
        package = ".".join(["decoy_engine", "quality", *relative.parts[:-1]])
        for line, name in _imported_modules(ast.parse(path.read_text()), package):
            if name == "decoy_engine.generation" or name.startswith("decoy_engine.generation."):
                found.append(f"{path.relative_to(root)}:{line} {name}")
    return found


def test_only_score_pairs_builds_a_pair_score() -> None:
    assert scan_pair_score_builders(_COPULA) == []


def test_report_reads_only_public_quality_modules() -> None:
    assert scan_private_quality_imports(_COPULA) == []


def test_quality_never_imports_generation() -> None:
    assert scan_quality_to_generation(_QUALITY) == []


@pytest.mark.parametrize(
    "source",
    [
        "from decoy_engine.generation.copula._quality_pairwise import PairScore\n"
        "def f():\n    return PairScore(('a', 'b'), 'm', 'scored', None, 1.0)\n",
        "import dataclasses\ndef f(p):\n    return dataclasses.replace(p, score=1.0)\n",
        "from dataclasses import replace\ndef f(p):\n    return replace(p, score=1.0)\n",
        "import dataclasses as dc\ndef f(p):\n    return dc.replace(p, score=1.0)\n",
        "def f(p):\n    object.__setattr__(p, 'score', 1.0)\n",
        "from decoy_engine.generation.copula._quality_pairwise import PairScore as PS\n"
        "def f():\n    return PS(('a', 'b'), 'm', 'scored', None, 1.0)\n",
        "FORGED = PairScore(('a', 'b'), 'm', 'scored', None, 1.0)\n",
        "async def f():\n    return PairScore(('a', 'b'), 'm', 'scored', None, 1.0)\n",
        "forge = lambda: PairScore(('a', 'b'), 'm', 'scored', None, 1.0)\n",
        "import dataclasses as d2\ndef f(p):\n    return d2.replace(p, score=1.0)\n",
        "Builder = PairScore\n",
        "from decoy_engine.generation.copula import _quality_pairwise as qp\n"
        "def f():\n    return qp.PairScore(('a', 'b'), 'm', 'scored', None, 1.0)\n",
    ],
)
def test_builder_scanner_sees_seeded_violations(tmp_path: Path, source: str) -> None:
    for path in _report_modules(_COPULA):
        shutil.copy(path, tmp_path / path.name)
    (tmp_path / "_quality_report.py").write_text(
        (tmp_path / "_quality_report.py").read_text() + "\n" + source
    )
    assert scan_pair_score_builders(tmp_path)


@pytest.mark.parametrize(
    "source",
    [
        "from decoy_engine.quality._dcr import compute_dcr\n",
        "import decoy_engine.quality._attacks as attacks\n",
        "from decoy_engine.quality import _dcr\n",
        "import importlib\nx = importlib.import_module('decoy_engine.quality._dcr')\n",
    ],
)
def test_private_import_scanner_sees_seeded_violations(tmp_path: Path, source: str) -> None:
    (tmp_path / "_quality_extra.py").write_text(source)
    assert scan_private_quality_imports(tmp_path)


@pytest.mark.parametrize(
    "source",
    [
        "from decoy_engine.generation import copula\n",
        "import decoy_engine.generation.copula as c\n",
        "from decoy_engine.generation.copula._fit import fit_gaussian_copula\n",
        "from ..generation import copula\n",
        "import importlib\nm = importlib.import_module('decoy_engine.generation.copula')\n",
    ],
)
def test_one_way_scanner_sees_seeded_violations(tmp_path: Path, source: str) -> None:
    shutil.copytree(_QUALITY, tmp_path / "quality", ignore=shutil.ignore_patterns("__pycache__"))
    target = tmp_path / "quality" / "fidelity.py"
    target.write_text(target.read_text() + "\n" + source)
    assert scan_quality_to_generation(tmp_path / "quality")

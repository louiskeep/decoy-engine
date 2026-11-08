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


def scan_pair_score_builders(root: Path) -> list[str]:
    """`PairScore(...)` outside `score_pairs`, any `replace(...)`, any `object.__setattr__`."""
    found: list[str] = []
    for path in _report_modules(root):
        tree = ast.parse(path.read_text())
        for function in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            for node in ast.walk(function):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                where = f"{path.name}:{node.lineno}"
                if name == "PairScore" and not (
                    path.name == "_quality_pairwise.py" and function.name == "score_pairs"
                ):
                    found.append(f"{where} PairScore built outside score_pairs")
                if name == "replace" and not isinstance(getattr(func, "value", None), ast.Constant):
                    if isinstance(func, ast.Name) or getattr(func.value, "id", "") in {
                        "dataclasses",
                        "dc",
                    }:
                        found.append(f"{where} dataclasses.replace")
                if name == "__setattr__":
                    found.append(f"{where} __setattr__")
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "dataclasses":
                if any(alias.name == "replace" for alias in node.names):
                    found.append(f"{path.name}:{node.lineno} imports dataclasses.replace")
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

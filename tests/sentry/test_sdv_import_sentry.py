"""SDV sentry: engine code never imports SDV or sdmetrics, and no citation calls SDV permissive.

Class: untrusted producer / license boundary. SDV is BUSL-1.1 with an exclusion for a
Synthetic Data Service offered to third parties, so the engine may cite the HMA1 method
(Patki et al., 2016) but never depend on SDV code. The import scan is an AST walk (not
grep) so the word ``sdv`` in comments and docstrings stays legal while real imports,
``importlib.import_module("sdv...")`` and ``__import__("sdv")`` literals are caught.
Pattern: port of decoy-platform tests/sentry/test_sdv_import_sentry.py.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("src", "scripts", "testflight", "custom_providers", "examples")
FORBIDDEN = ("sdv", "sdmetrics")
_DYNAMIC = {"import_module", "__import__"}


def _forbidden(module: str | None) -> bool:
    return bool(module) and module.split(".")[0] in FORBIDDEN


def scan_source(source: str, filename: str = "<src>") -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source, filename)):
        if isinstance(node, ast.Import):
            hits += [(node.lineno, a.name) for a in node.names if _forbidden(a.name)]
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and _forbidden(node.module):
                hits.append((node.lineno, node.module or ""))
        elif isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name in _DYNAMIC and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if _forbidden(arg.value):
                        hits.append((node.lineno, arg.value))
    return hits


def _scan_tree(root: Path = ROOT) -> list[str]:
    out = []
    for d in SCAN_DIRS:
        for path in sorted((root / d).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            for line, mod in scan_source(path.read_text(encoding="utf-8"), str(path)):
                out.append(f"{path.relative_to(root).as_posix()}:{line} imports {mod}")
    return out


def test_no_sdv_or_sdmetrics_import_in_engine_code():
    assert _scan_tree() == []


def test_scan_dirs_cover_the_package():
    assert (ROOT / "src" / "decoy_engine" / "__init__.py").is_file()


def test_seeded_module_under_src_is_flagged(tmp_path):
    pkg = tmp_path / "src" / "decoy_engine"
    pkg.mkdir(parents=True)
    fixture = ROOT / "tests/fixtures/license_gate/sdv_importer.py.txt"
    (pkg / "seeded.py").write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    (pkg / "clean.py").write_text("# import sdv\n'''uses the SDV HMA1 method'''\n")
    assert _scan_tree(tmp_path) == ["src/decoy_engine/seeded.py:3 imports sdv.multi_table"]


@pytest.mark.parametrize(
    "src",
    [
        "import sdv",
        "import sdv.single_table",
        "import os, sdmetrics",
        "from sdv.metadata import SingleTableMetadata",
        "from sdmetrics.reports import x",
        "import importlib\nimportlib.import_module('sdv.multi_table')",
        "from importlib import import_module\nimport_module('sdmetrics')",
        "__import__('sdv')",
        "def f():\n    import sdv.lite",
    ],
)
def test_scanner_catches_seeded_imports(src):
    assert scan_source(src)


@pytest.mark.parametrize(
    "src",
    [
        "# import sdv\nx = 'sdv'",
        '"""Pattern: SDV HMA1 method (Patki et al., 2016)."""',
        "import sdvx",
        "from . import sdv",
        "import importlib\nimportlib.import_module(name)",
    ],
)
def test_scanner_ignores_comments_docstrings_and_lookalikes(src):
    assert scan_source(src) == []


def _names(text: str) -> set[str]:
    found = set()
    for m in re.finditer(
        r"(?im)^\s*\"?([a-z0-9][a-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(?:==|>=|<=|~=|!=|<|>|@|\\|\"|,|$)",
        text,
    ):
        found.add(re.sub(r"[-_.]+", "-", m.group(1)).lower())
    return found


def test_sdv_is_in_neither_the_lock_nor_any_extra():
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    locked = {
        re.sub(r"[-_.]+", "-", n).lower() for n in re.findall(r'(?m)^name = "([^"]+)"$', lock)
    }
    assert len(locked) > 50, "lock parse found too few packages"
    assert not locked & set(FORBIDDEN)
    assert not _names((ROOT / "pyproject.toml").read_text(encoding="utf-8")) & set(FORBIDDEN)


# --- citation labels ------------------------------------------------------------------
# A citation that labels SDV permissive misstates what the engine may ship. The label
# and the SDV mention may sit on adjacent lines, so a line pair is checked; docs/archive
# is history and is not rewritten.
_LICENSE_SCAN = ("src", "scripts", "tests", "docs", "testflight", "custom_providers", "examples")
_LICENSE_SKIP = ("docs/archive/",)
_LICENSE_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".txt", ".toml", ".rst", ".cfg", ".json"}
_SDV_WORD = re.compile(r"(?<![A-Za-z])SDV(?![A-Za-z])|sdv-dev/SDV")
_PERMISSIVE = re.compile(r"\b(MIT|Apache|BSD|ISC|PSF)\b")
_SELF = Path(__file__).resolve()


def sdv_license_claims(text: str) -> list[int]:
    lines = text.splitlines()
    out = []
    for n, line in enumerate(lines, 1):
        if not _SDV_WORD.search(line):
            continue
        pair = line + "\n" + (lines[n] if n < len(lines) else "")
        if _PERMISSIVE.search(pair) and "BUSL" not in pair:
            out.append(n)
    return out


def _claim_files(root: Path = ROOT) -> list[Path]:
    files = sorted(p for p in root.glob("*.md") if p.is_file())
    files += [root / "pyproject.toml", root / "NOTICE"]
    for d in _LICENSE_SCAN:
        files += sorted(p for p in (root / d).rglob("*") if p.is_file())
    return [p for p in files if p.suffix in _LICENSE_SUFFIXES or p.name == "NOTICE"]


def _license_claim_tree(root: Path = ROOT) -> list[str]:
    out = []
    for path in _claim_files(root):
        rel = path.relative_to(root).as_posix()
        if not path.is_file() or path.resolve() == _SELF or rel.startswith(_LICENSE_SKIP):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        out += [f"{rel}:{n}" for n in sdv_license_claims(text)]
    return sorted(set(out))


def test_no_citation_labels_sdv_permissive():
    assert _license_claim_tree() == []


def test_claim_scan_reaches_root_changelog_and_src():
    rels = {p.relative_to(ROOT).as_posix() for p in _claim_files()}
    assert {"CHANGELOG.md", "NOTICE", "pyproject.toml", "docs/methodology-registry.yaml"} <= rels
    assert "src/decoy_engine/validators/_fk_validators.py" in rels


@pytest.mark.parametrize(
    "text",
    [
        "Pattern: SDV HMA1 (sdv-dev/SDV, MIT). Parent-first DAG;",
        "    source: sdv-dev/SDV (MIT)",
        "# SDV (Apache-2.0) HMA1",
        "pandas 2.x (Apache-2.0); SDV (MIT); RFC 5869",
        "https://sdv.dev/SDV/user_guides/timeseries/index.html, MIT License). Per-",
        "follows SDV per-group sequencing\n(MIT License)",
    ],
)
def test_license_claim_scanner_catches_seeded_lines(text):
    assert sdv_license_claims(text) == [1]


@pytest.mark.parametrize(
    "text",
    [
        "Pattern: SDV HMA1 method (Patki et al., 2016; method only, no SDV code: SDV is BUSL-1.1).",
        "SDMetrics `MLEfficacy` (sdmetrics package, MIT)",
        "we use SDV's HMA1 pattern",
        "SDVX (MIT)",
        "SDV HMA1 method (Patki et al., 2016;\nno SDV code is used; SDV is BUSL-1.1, not MIT)",
    ],
)
def test_license_claim_scanner_ignores_correct_citations(text):
    assert sdv_license_claims(text) == []

"""Class 1 sentry: no Python ``re`` pattern ends with ``$``.

``$`` matches before a trailing newline, so ``"name\\n"`` passes a ``^...$`` identifier check and
then reaches SQL text, a path or a version compare. ``\\Z`` (or ``re.fullmatch``) does not.
The scan is pure AST and resolves ``re`` aliases, ``from re import ...`` and module-level
pattern constants. Pydantic ``pattern=`` is exempt only while no model asks for the stdlib
engine, because pydantic-core's default Rust engine anchors ``$`` at the end of the text.
"""

from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path

import pytest

from tests.sentry.defects import _defect_scan as scan

SRC = scan.SRC


@pytest.fixture(scope="module")
def index() -> scan.ModuleIndex:
    return scan.ModuleIndex()


def _tree(tmp_path: Path, files: dict[str, str]) -> scan.ModuleIndex:
    pkg = tmp_path / "decoy_engine"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    for name, text in files.items():
        (pkg / name).write_text(text)
    return scan.ModuleIndex(pkg)


def _flagged(tmp_path: Path, text: str, **extra: str) -> list[str]:
    anchored, _ = scan.end_anchor_sites(_tree(tmp_path, {"mod.py": text, **extra}))
    return [f"{s.file}:{s.line}" for s in anchored]


# ----- the real tree ------------------------------------------------------------------------


def test_no_end_anchor_in_a_python_re_pattern(index: scan.ModuleIndex) -> None:
    anchored, _ = scan.end_anchor_sites(index)
    assert [f"{s.file}:{s.line}" for s in anchored] == []


def test_no_model_selects_the_stdlib_regex_engine(index: scan.ModuleIndex) -> None:
    assert scan.python_re_engine_sites(index) == []


def test_the_scan_sees_the_re_calls_it_claims_to_cover(index: scan.ModuleIndex) -> None:
    calls = 0
    for path, tree in index.trees.items():
        names = scan._ReNames(index, scan.module_name(path, index.src))
        calls += sum(
            1 for n in ast.walk(tree) if isinstance(n, ast.Call) and names.function(n.func)
        )
    assert calls >= 100, calls


# ----- seeded bypasses: each spelling must turn the scan red -------------------------------


@pytest.mark.parametrize(
    "text",
    [
        'import re\nX = re.compile("a$")\n',
        'import re as r\nX = r.compile("a$")\n',
        'from re import compile as c\nX = c("a$")\n',
        'from re import match\nmatch("a$", "a")\n',
        'import re\nP = "a$"\nX = re.compile(P)\n',
        'import re\nP = "a"\nX = re.compile(P + "$")\n',
        'import re\nbody = "x"\nX = re.compile(rf"^({body})$")\n',
        'import re\nX = re.compile("(" + "|".join(["a"]) + ")$")\n',
        'import re\nX = re.compile("^(?:a|b$)")\n',
        'import re\nX = re.sub(r"\\(\\d+\\)$", "", "a")\n',
    ],
)
def test_seeded_end_anchor_is_flagged(tmp_path: Path, text: str) -> None:
    assert _flagged(tmp_path, text) != []


def test_a_constant_imported_from_another_module_is_followed(tmp_path: Path) -> None:
    found = _flagged(
        tmp_path,
        "import re\nfrom decoy_engine.consts import P\nX = re.compile(P)\n",
        **{"consts.py": 'P = "a$"\n'},
    )
    assert found == ["decoy_engine/mod.py:3"]


@pytest.mark.parametrize(
    "text",
    [
        'import re\nX = re.compile(r"a\\Z")\n',
        'import re\nX = re.compile("[a$]")\n',
        'import re\nX = re.compile(r"a\\$")\n',
        'import re\nX = re.compile("a$", re.MULTILINE)\n',
        'class re:\n    @staticmethod\n    def compile(x): ...\nX = re.compile("a$")\n',
        'X = "a$"\n',
    ],
)
def test_seeded_safe_forms_are_not_flagged(tmp_path: Path, text: str) -> None:
    assert _flagged(tmp_path, text) == []


def test_restoring_the_dollar_in_a_real_validator_turns_the_scan_red(tmp_path: Path) -> None:
    real = (SRC / "connectors" / "postgres_snapshot.py").read_text(encoding="utf-8")
    assert '_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}\\Z")' in real
    mutated = real.replace('{0,62}\\Z")', '{0,62}$")', 1)
    assert mutated != real
    assert _flagged(tmp_path, mutated) != []


def test_a_stdlib_engine_request_is_flagged(tmp_path: Path) -> None:
    text = 'from pydantic import ConfigDict\nC = ConfigDict(regex_engine="python-re")\n'
    assert scan.python_re_engine_sites(_tree(tmp_path, {"mod.py": text})) != []


# ----- AT-B1: each fixed validator refuses value + "\n" ------------------------------------

# (module, attribute, a value the pattern accepts). The "\n" twin must be refused.
_FIXED = [
    ("decoy_engine.checksums", "_NPI_RE", "1234567890"),
    ("decoy_engine.connectors.postgres_snapshot", "_IDENT_RE", "orders_1"),
    ("decoy_engine.connectors.postgres_snapshot", "_NUMERIC_RE", "numeric(10,2)"),
    ("decoy_engine.connectors.postgres_snapshot", "_LEN_SUFFIX_RE", "(10)"),
    ("decoy_engine.execution._chunked_fk_dtype_safety", "_TIMESTAMP_DECLARED_RE", "timestamp[us]"),
    (
        "decoy_engine.execution._chunked_fk_dtype_safety",
        "_DECIMAL_WITH_WIDTH_DECLARED_RE",
        "decimal128(10, 2)",
    ),
    (
        "decoy_engine.execution._chunked_fk_dtype_safety",
        "_DECIMAL_BARE_WIDTH_DECLARED_RE",
        "decimal(10, 2)",
    ),
    ("decoy_engine.execution._chunked_fk_vocab", "_DECIMAL_PRECISION_SCALE_RE", "decimal(10, 2)"),
    ("decoy_engine.execution.native._requirements", "_TZ_DATETIME_LABEL", "datetime64[ns, UTC]"),
    ("decoy_engine.execution.out_of_core._duckdb", "_MEMORY_LIMIT_MB_PATTERN", "512MB"),
    ("decoy_engine.internal.pandas_compat", "_DATETIME_RES", "datetime64[ns]"),
    ("decoy_engine.profiling.sentinels", "_ISO_DATE_RE", "2024-01-31"),
    ("decoy_engine.secret_providers._types", "_VERSION_RE", "12"),
    ("decoy_engine.secret_providers._types", "_LOGICAL_VERSION_RE", "v1.2-a"),
    ("decoy_engine.quality._freetext_names", "_FREETEXT_NAME_PATTERN", "summary"),
]


@pytest.mark.parametrize(("module", "attr", "good"), _FIXED, ids=lambda v: str(v)[-32:])
def test_fixed_validator_refuses_a_trailing_newline(module: str, attr: str, good: str) -> None:
    pattern: re.Pattern[str] = getattr(importlib.import_module(module), attr)
    assert pattern.search(good) is not None
    assert pattern.search(good + "\n") is None


def test_the_hint_pattern_builder_refuses_a_trailing_newline() -> None:
    from decoy_engine.detection import detectors

    compiled = detectors._hint(["email"])
    assert compiled.match("user_email") is not None
    assert compiled.match("user_email\n") is None

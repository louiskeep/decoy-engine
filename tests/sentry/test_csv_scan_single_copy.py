"""AT-E8: the stdlib csv parser runs in one engine module, so there is one width scan.
AT-E12 (below): the strict-policy parser-error pass-through has a closed set of callers.

`decoy_engine.profile._csv_reader` is the only caller of `csv.reader` / `csv.DictReader`.
A second caller would be a second copy of the headerless scan, or a CSV read that skips the
dialect and the fail-closed rules. One other module reads bundled data, not user files.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).parents[2] / "src" / "decoy_engine"
READER = "profile/_csv_reader.py"
# A DictReader over bundled reference data, never a user's source file.
BUNDLED_DATA_READER = "generation/composite/_city_state_zip.py"
_CALLS = {"reader", "DictReader"}


def csv_reader_calls(source: str) -> list[int]:
    """Line numbers of calls to csv.reader / csv.DictReader, through any import alias."""
    tree = ast.parse(source)
    module_aliases: set[str] = set()
    direct: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            module_aliases.update(a.asname or a.name for a in node.names if a.name == "csv")
        elif isinstance(node, ast.ImportFrom) and node.module == "csv":
            direct.update(a.asname or a.name for a in node.names if a.name in _CALLS)
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in _CALLS
            and isinstance(func.value, ast.Name)
            and func.value.id in module_aliases
        ) or (isinstance(func, ast.Name) and func.id in direct):
            lines.append(node.lineno)
    return lines


def test_csv_reader_only_in_csv_reader_module() -> None:
    offenders = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel in {READER, BUNDLED_DATA_READER}:
            continue
        lines = csv_reader_calls(path.read_text(encoding="utf-8"))
        if lines:
            offenders[rel] = lines
    assert not offenders, f"csv parser used outside {READER}: {offenders}"


def test_the_sentry_sees_the_reader_and_the_named_one_off() -> None:
    assert csv_reader_calls((SRC / READER).read_text(encoding="utf-8"))
    assert csv_reader_calls((SRC / BUNDLED_DATA_READER).read_text(encoding="utf-8"))


def test_the_walk_sees_a_seeded_bypass() -> None:
    aliased = "import csv as c\nrows = c.reader(open('x'))\n"
    direct = "from csv import DictReader as D\nrows = D(open('x'))\n"
    assert csv_reader_calls(aliased) == [2]
    assert csv_reader_calls(direct) == [2]
    assert csv_reader_calls("import csv\ncsv.writer(None)\n") == []


# AT-E12: the strict-policy pass-through has a closed set of callers.
#
# `guarded_with_parser_errors` lets a recognized pandas parser error through a strict policy,
# and its text can carry a cell. A caller must replace it with its own value-free refusal, so a
# new caller is a new place that has to do so: it needs a reviewed change here, not a silent import.
PASS_THROUGH_CALLERS: dict[str, frozenset[str]] = {
    # the helper, `guarded`, `guarded_iter`, and the pandas adapter that wraps it
    "guarded_with_parser_errors": frozenset(
        {"fileaccess/_errors.py", "fileaccess/adapters/pandas.py"}
    ),
    # the adapter defines the reads; only the CSV reader turns their errors into a refusal
    "read_csv_parser_errors": frozenset({"fileaccess/adapters/pandas.py", READER}),
    "read_csv_chunks_parser_errors": frozenset({"fileaccess/adapters/pandas.py", READER}),
}


def pass_through_references(source: str, names: frozenset[str]) -> dict[str, list[int]]:
    """Lines that name one of `names`: a call, attribute access, import or definition."""
    found: dict[str, list[int]] = {}
    for node in ast.walk(ast.parse(source)):
        hit: str | None = None
        if isinstance(node, ast.Name) and node.id in names:
            hit = node.id
        elif isinstance(node, ast.Attribute) and node.attr in names:
            hit = node.attr
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            hit = node.name  # a definition: a second copy of the pass-through is a new caller
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in names:
                    found.setdefault(alias.name, []).append(node.lineno)
        if hit is not None:
            found.setdefault(hit, []).append(node.lineno)
    return found


def test_pass_through_callers_are_the_allowlisted_modules() -> None:
    names = frozenset(PASS_THROUGH_CALLERS)
    offenders: dict[str, list[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        for name in pass_through_references(path.read_text(encoding="utf-8"), names):
            if rel not in PASS_THROUGH_CALLERS[name]:
                offenders.setdefault(name, []).append(rel)
    assert not offenders, f"strict-policy pass-through used outside its callers: {offenders}"


def test_the_allowlist_has_no_stale_entry() -> None:
    names = frozenset(PASS_THROUGH_CALLERS)
    for name, modules in PASS_THROUGH_CALLERS.items():
        for rel in modules:
            refs = pass_through_references((SRC / rel).read_text(encoding="utf-8"), names)
            assert name in refs, f"{rel} no longer names {name}; shrink the allowlist"


def test_the_pass_through_walk_sees_a_seeded_caller() -> None:
    names = frozenset(PASS_THROUGH_CALLERS)
    imported = "from decoy_engine.fileaccess._errors import guarded_with_parser_errors as g\n"
    attribute = "from x import pandas as p\np.read_csv_parser_errors(h)\n"
    plain = "from decoy_engine.fileaccess.adapters.pandas import read_csv_chunks_parser_errors\n"
    assert pass_through_references(imported, names) == {"guarded_with_parser_errors": [1]}
    assert pass_through_references(attribute, names) == {"read_csv_parser_errors": [2]}
    assert pass_through_references(plain, names) == {"read_csv_chunks_parser_errors": [1]}
    assert pass_through_references("import os\nos.path.join('a')\n", names) == {}

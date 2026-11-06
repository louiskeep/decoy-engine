"""AT-E8: the stdlib csv parser runs in one engine module, so there is one width scan.

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

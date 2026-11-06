"""AT-E11: FileSource and CsvDialect share one set of CSV-dialect rules."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from decoy_engine.config import FileSource
from decoy_engine.config import _sources as sources_module
from decoy_engine.config._csv_dialect import CsvDialect, csv_dialect_problem
from decoy_engine.errors import ConfigError

# (has_header, column_names, delimiter)
INVALID: dict[str, tuple[bool, Any, str]] = {
    "header_with_names": (True, ["a"], ","),
    "headerless_none": (False, None, ","),
    "headerless_empty": (False, [], ","),
    "blank_name": (False, ["a", " "], ","),
    "duplicate_name": (False, ["a", "a"], ","),
    "two_char_delimiter": (True, None, ",,"),
    "empty_delimiter": (True, None, ""),
    "lf_delimiter": (True, None, "\n"),
    "cr_delimiter": (True, None, "\r"),
    "nul_delimiter": (True, None, "\x00"),
    "control_delimiter": (True, None, "\x07"),
}
VALID: dict[str, tuple[bool, Any, str]] = {
    "defaults": (True, None, ","),
    "tab": (True, None, "\t"),
    "pipe": (True, None, "|"),
    "headerless_with_names": (False, ["a", "b"], ","),
}


def _file_source(has_header: bool, names: Any, delimiter: str) -> FileSource:
    raw: dict[str, Any] = {
        "type": "file",
        "format": "csv",
        "path": "a.csv",
        "has_header": has_header,
        "delimiter": delimiter,
    }
    if names is not None:
        raw["column_names"] = names
    return FileSource.model_validate(raw)


@pytest.mark.parametrize("case", list(INVALID))
def test_filesource_and_csvdialect_reject_the_same_dialects(case: str) -> None:
    has_header, names, delimiter = INVALID[case]
    problem = csv_dialect_problem(has_header, names, delimiter)
    assert problem is not None
    with pytest.raises(ValueError) as from_source:
        _file_source(has_header, names, delimiter)
    assert f"FileSource: {problem}" in str(from_source.value)
    with pytest.raises(ConfigError) as from_dialect:
        CsvDialect(has_header, None if names is None else tuple(names), delimiter)
    assert str(from_dialect.value) == f"CsvDialect: {problem}"


@pytest.mark.parametrize("case", list(VALID))
def test_both_accept_the_valid_dialects(case: str) -> None:
    has_header, names, delimiter = VALID[case]
    assert csv_dialect_problem(has_header, names, delimiter) is None
    _file_source(has_header, names, delimiter)
    CsvDialect(has_header, None if names is None else tuple(names), delimiter)


def test_csvdialect_normalizes_names_to_a_tuple() -> None:
    assert CsvDialect(False, ["a", "b"]).column_names == ("a", "b")  # type: ignore[arg-type]


def test_filesource_has_no_dialect_rule_of_its_own() -> None:
    tree = ast.parse(Path(sources_module.__file__).read_text(encoding="utf-8"))
    method = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_validate_delimited_params"
    )
    # Every read of the dialect fields must be a direct argument of csv_dialect_problem.
    allowed: set[int] = set()
    for node in ast.walk(method):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "csv_dialect_problem"
        ):
            allowed.update(id(arg) for arg in node.args)
    reads = [
        n
        for n in ast.walk(method)
        if isinstance(n, ast.Attribute) and n.attr in {"column_names", "delimiter", "has_header"}
    ]
    stray = [n for n in reads if id(n) not in allowed]
    assert reads, "the csv branch must pass the dialect fields to csv_dialect_problem"
    assert not stray, (
        f"dialect fields read outside csv_dialect_problem at lines {[n.lineno for n in stray]}"
    )

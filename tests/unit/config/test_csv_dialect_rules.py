"""AT-E11: FileSource and CsvDialect share one set of CSV-dialect rules."""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path
from typing import Any

import pytest

from decoy_engine.config import FileSource
from decoy_engine.config import _sources as sources_module
from decoy_engine.config._csv_dialect import (
    BOTH_NAME_SOURCES,
    CSV_DIALECT_DEFAULTS,
    DIALECT_PROBLEM_CODES,
    HEADERLESS_WITHOUT_NAMES,
    NAMES_WITH_HEADER,
    CsvDialect,
    csv_dialect_problem,
)
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


# Fix round: value-free public errors, one home for defaults, no str column_names.

SECRET = "SECRETMARK"


def _assert_no_secret(err: BaseException) -> None:
    for text in (str(err), repr(err), repr(err.args)):
        assert SECRET not in text
    assert err.__cause__ is None and err.__context__ is None


@pytest.mark.parametrize("bad", [SECRET, SECRET[:2], "\x07"], ids=["long", "two", "control"])
def test_public_delimiter_errors_are_value_free(bad: str) -> None:
    from decoy_engine.profile._csv_reader import resolve_csv_dialect

    with pytest.raises(ConfigError) as direct:
        CsvDialect(True, None, bad)
    with pytest.raises(ConfigError) as resolved:
        resolve_csv_dialect({"has_header": True, "delimiter": bad})
    for err in (direct.value, resolved.value):
        _assert_no_secret(err)
        assert repr(bad) not in str(err) and repr(bad) not in repr(err)
    assert csv_dialect_problem(True, None, bad) is not None
    assert repr(bad) not in (csv_dialect_problem(True, None, bad) or "")


def test_csvdialect_defaults_equal_the_filesource_defaults() -> None:
    import dataclasses

    fields = {f.name: f.default for f in dataclasses.fields(CsvDialect)}
    for name in ("column_names", "delimiter"):
        assert FileSource.model_fields[name].default == fields[name], name
    # FileSource's `None` means "not given" (refused for csv), not a dialect default.
    assert FileSource.model_fields["has_header"].default is None
    assert CsvDialect().has_header == CSV_DIALECT_DEFAULTS.has_header


def test_dialect_defaults_have_one_home() -> None:
    """The descriptor owns the values: neither consumer restates a default literal."""
    assert CSV_DIALECT_DEFAULTS._fields == tuple(f.name for f in dataclasses.fields(CsvDialect))
    for name in CSV_DIALECT_DEFAULTS._fields:
        assert getattr(CsvDialect(), name) == getattr(CSV_DIALECT_DEFAULTS, name)
        if name != "has_header":
            assert FileSource.model_fields[name].default == getattr(CSV_DIALECT_DEFAULTS, name)
    # Neither consumer restates a default as a literal: each reads the descriptor.
    from decoy_engine.config import _csv_dialect as dialect_module

    restated: list[str] = []
    for module, cls_name in ((sources_module, "FileSource"), (dialect_module, "CsvDialect")):
        tree = ast.parse(Path(module.__file__).read_text())
        cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == cls_name)
        for node in cls.body:
            if not (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)):
                continue
            if node.target.id not in CSV_DIALECT_DEFAULTS._fields or node.value is None:
                continue
            if (cls_name, node.target.id) == ("FileSource", "has_header"):
                continue  # its `None` means "not given", not a default
            value = node.value
            if isinstance(value, ast.Call):  # Field(default=...)
                value = next((k.value for k in value.keywords if k.arg == "default"), value)
            if isinstance(value, ast.Constant):
                restated.append(f"{cls_name}.{node.target.id}")
    assert not restated, f"default literal restated outside the descriptor: {restated}"
    for node in ast.walk(ast.parse(Path(sources_module.__file__).read_text())):
        if isinstance(node, ast.Compare) and "data.get" in ast.unparse(node):
            assert not any(isinstance(c, ast.Constant) for c in node.comparators), ast.unparse(node)


def test_filesource_dump_omits_exactly_the_descriptor_defaults() -> None:
    plain = FileSource.model_validate(
        {"type": "file", "format": "csv", "path": "a.csv", "has_header": True}
    )
    assert set(plain.model_dump()) == {"type", "format", "path", "layout", "has_header"}
    tab = FileSource.model_validate(
        {
            "type": "file",
            "format": "csv",
            "path": "a.csv",
            "delimiter": "\t",
            "has_header": False,
            "column_names": ["a"],
        }
    )
    assert {"delimiter", "has_header", "column_names"} <= set(tab.model_dump())


@pytest.mark.parametrize("bad", [SECRET, SECRET[:2]], ids=["long", "two"])
def test_filesource_validation_error_is_value_free(bad: str) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as info:
        FileSource.model_validate(
            {"type": "file", "format": "csv", "path": "p", "has_header": True, "delimiter": bad}
        )
    err = info.value
    for text in (str(err), repr(err), repr(err.args)):
        assert bad not in text


def _pipeline_with_delimiter(delimiter: str) -> dict[str, Any]:
    return {
        "version": 2,
        "global_settings": {"seed": 1},
        "sources": {
            "s": {
                "type": "file",
                "format": "csv",
                "has_header": True,
                "path": "p.csv",
                "delimiter": delimiter,
            }
        },
        "tables": [{"name": "t", "source": "s", "columns": []}],
        "targets": {"o": {"type": "file", "format": "csv", "path": "o.csv"}},
    }


def test_public_loader_route_carries_no_submitted_delimiter() -> None:
    """`validate_config` is the public config-error boundary: only value-free text leaves it."""
    from decoy_engine import PipelineValidationError, validate_config

    with pytest.raises(PipelineValidationError) as info:
        validate_config(_pipeline_with_delimiter(SECRET))
    err = info.value
    assert "delimiter" in str(err)  # the real problem is still reported
    assert "sources.s.file" in str(err)  # and located by the source name
    _assert_no_secret(err)  # str, repr, args, and no chained pydantic error carrying input


def test_source_models_hide_input_in_errors() -> None:
    """Class guard: every source descriptor hides input, as the pipeline models do."""
    from decoy_engine.config import FileSource, GCSSource, S3Source

    for model in (FileSource, S3Source, GCSSource):
        assert model.model_config.get("hide_input_in_errors") is True, model.__name__


def test_resolve_passes_only_the_keys_present() -> None:
    from decoy_engine.profile._csv_reader import resolve_csv_dialect

    plain = FileSource.model_validate(
        {"type": "file", "format": "csv", "path": "a.csv", "has_header": True}
    )
    assert resolve_csv_dialect(plain.model_dump()) == CsvDialect(
        plain.has_header, plain.column_names, plain.delimiter
    )
    assert resolve_csv_dialect({"has_header": True}) == CsvDialect()
    with pytest.raises(ConfigError):
        resolve_csv_dialect({})


@pytest.mark.parametrize(
    "bad",
    ["ab", b"ab", 5, ("a", 1), iter(["a"])],
    ids=["str", "bytes", "int", "tuple-with-int", "iterator"],
)
def test_non_sequence_or_non_str_column_names_are_refused(bad: Any) -> None:
    from decoy_engine.profile._csv_reader import resolve_csv_dialect

    with pytest.raises(ConfigError):
        CsvDialect(False, bad)
    with pytest.raises(ConfigError):
        resolve_csv_dialect({"has_header": False, "column_names": bad})


@pytest.mark.parametrize(
    ("args", "kwargs", "text"),
    [
        ((False, ["a"], ","), {"column_names_file": "n.txt"}, BOTH_NAME_SOURCES),
        ((True, ["a"], ","), {}, NAMES_WITH_HEADER),
        ((False, None, ","), {}, HEADERLESS_WITHOUT_NAMES),
    ],
)
def test_dialect_problem_codes_match_returned_text(
    args: tuple[Any, ...], kwargs: dict[str, Any], text: str
) -> None:
    assert csv_dialect_problem(*args, **kwargs) == text
    assert text in DIALECT_PROBLEM_CODES
